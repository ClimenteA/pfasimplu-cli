import datetime
import re
import uuid
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

from google import genai
from google.genai import types
from joblib import Parallel, delayed
from loguru import logger as log
from pydantic import BaseModel, Field, model_validator
from pypdf import PdfReader
from tenacity import retry, stop_after_attempt, stop_after_delay, wait_random
from unidecode import unidecode

from pfasimplu_cli.utils import (
    create_excel,
    extract_json_from_llm_response,
    jitter_call,
    make_workdir,
    safe_rmtree,
)

from .common import (
    ModalitatePlata,
    RaspunsRaport,
    get_data_from_excel,
    get_model_hash,
)
from .extrase_bancare import ExtrasBancarLineSchema
from .fisa_mijloc_fix import creeaza_fisa_mijloc_fix


class PDFZIPGroup(BaseModel):
    pdf_name: str
    pdf_path: str
    zip_name: str
    zip_path: str
    efactura_xml: str


# TODO - noi presupunem ca efacturile sunt in RON, dar pot fi si in alte monede.
class EFacturaSchema(BaseModel):
    # ANAF ID
    nume_fisier: Optional[str] = Field(
        default=None, description="Nu completa acest camp."
    )
    # Date Factura
    numar_factura: str = Field(
        description="Numarul de identificare al facturii ex: din <cbc:ID>ELW201754768</cbc:ID> extragem ELW201754768"
    )
    data_emitere: date = Field(description="Data emiterii facturii IssueDate")
    data_scadenta: date = Field(description="Data emiterii facturii DueDate")
    total_de_plata: float = Field(description="Total de plata PayableAmount")
    valoare_tva: float = Field(
        description="Total de plata PayableAmount cac:TaxTotal > cbc:TaxAmount"
    )
    modalitate_plata: ModalitatePlata = Field(
        default=ModalitatePlata.CARD, description="Nu completa acest camp."
    )
    # Date vanzator
    vanzator_nume: str = Field(
        description="Numele vanzatorului/firmei care vinde AccountingSupplierParty > RegistrationName sau AccountingSupplierParty > Contact > Name"
    )
    vanzator_cui: str = Field(
        description="CUI-ul vanzatorului de forma RO14399840 sau 14399840 AccountingSupplierParty > PartyIdentification > ID sau daca nu este disponibil AccountingSupplierParty > PartyTaxScheme > CompanyID"
    )
    vanzator_cui_tva: Optional[str] = Field(
        default=None,
        description="Cod TVA al vanzatorului (daca exista) AccountingSupplierParty > PartyTaxScheme > CompanyID (PartyTaxScheme trebuie sa contina si <cbc:ID>VAT</cbc:ID>)",
    )
    vanzator_adresa: str = Field(
        description="Adresa vanzatorului AccountingSupplierParty > PostalAddress > CityName, StreetName"
    )
    # Date comparator
    cumparator_nume: str = Field(
        description="Numele cumparatorului AccountingCustomerParty > RegistrationName sau AccountingSupplierParty > Contact > Name"
    )
    cumparator_cui: str = Field(
        description="CUI-ul cumparatorului de forma RO14399840 sau 14399840 AccountingCustomerParty > PartyIdentification > ID sau daca nu este disponibil AccountingCustomerParty > PartyTaxScheme > CompanyID"
    )
    cumparator_cui_tva: Optional[str] = Field(
        default=None,
        description="CUI-ul cumparatorului de forma RO14399840 AccountingCustomerParty > PartyIdentification > ID sau daca nu este disponibil AccountingCustomerParty > PartyTaxScheme > CompanyID",
    )
    cumparator_adresa: str = Field(
        description="Adresa cumparatorului AccountingCustomerParty > PostalAddress > CityName, StreetName"
    )
    # Date produse servicii
    produse_sau_servicii: str = Field(
        description="O descriere scurta a serviciilor sau produselor achizitionate InvoiceLine (pot fi mai multe linii) ex: 'Lenovo Mouse Lenovo Essential, USB, 1600DPI, Negru / Rosu' > 'Lenovo Mouse Lenovo'"
    )
    obiect_de_inventar: Optional[bool] = Field(
        default=False,
        description=(
            "True pentru bunuri materiale principale, tangibile și reutilizabile, "
            "utilizate pe termen lung (>1 an) ca unități distincte în activitatea economică "
            "(ex: PC, Laptop, Monitor, Imprimantă, mobilier, aparatură). "
            "False pentru consumabile, accesorii minore (cabluri, mouse, tastatură), "
            "servicii sau resurse cloud, licențe software, certificate digitale "
            "(inclusiv token USB), abonamente sau orice element dat direct în consum."
        ),
    )
    mijloc_fix: Optional[bool] = Field(
        default=False, description="Nu completa acest camp."
    )
    nume_fisier_mijloc_fix: Optional[str] = Field(
        default=None, description="Nu completa acest camp."
    )
    data_tranzactie: Optional[date] = Field(
        default=None, description="Nu completa acest camp."
    )
    nume_fisier_extras_bancar: Optional[str] = Field(
        default=None, description="Nu completa acest camp."
    )

    @model_validator(mode="after")
    def post_process(self):
        self.vanzator_nume = unidecode(self.vanzator_nume)
        self.vanzator_adresa = unidecode(self.vanzator_adresa)
        self.cumparator_nume = unidecode(self.cumparator_nume)
        self.cumparator_adresa = unidecode(self.cumparator_adresa)
        self.produse_sau_servicii = unidecode(self.produse_sau_servicii)
        return self


class PZGroupBatch(BaseModel):
    pzgroup: PDFZIPGroup
    efactura: EFacturaSchema


@dataclass
class AgentEFacturi:
    zip_path: str
    cui_cif: str
    cod_tva: str
    zip_path_extrase_bancare: str | None = None
    model: str = "gemini-3.8-flash"
    client: genai.Client | None = None
    api_key: str | None = None

    def __post_init__(self):
        self.extracted_pdfs_and_zips = None
        self.pdfzip_groups = None
        if self.client is None:
            if not self.api_key:
                raise ValueError("AgentEFacturi requires a Gemini client or api_key")
            self.client = genai.Client(api_key=self.api_key)
        self.shared_dir = str(make_workdir("pfasimplu_efacturi_"))
        self.efacturi_path = Path(self.shared_dir) / "efacturi_colectate" / "efacturi"
        self.report_path = Path(self.shared_dir) / "efacturi_colectate"
        self.efacturi_path.mkdir(parents=True, exist_ok=True)

    def send_message(self, message: str):
        log.info(f"E-facturi: {message}")

    @property
    def system_prompt(self):
        prompt = """
        ## Rol de sistem:
        Ești un motor expert de extracție a datelor financiare (OCR/Parser), specializat în structura XML UBL 2.1 (e-Factura ANAF).
        Sarcina ta este să transformi un XML tehnic într-un JSON curat, respectând strict schema EFacturaSchema.

        ## Input:
        Vei primi un conținut XML brut.

        ## Output:
        Returnează DOAR un obiect JSON valid.

        ## Reguli Algoritmice de Extracție:

        ### 1. Părțile Implicate (Vânzător/Cumpărător)
        - **Nume:** Caută întâi în `<RegistrationName>`. Dacă lipsește, caută în `<Name>`.
        - **CUI (Identificator Fiscal):** - Extrage din `<PartyTaxScheme><CompanyID>` (ex: RO123456).
        - Dacă lipsește, caută în `<PartyIdentification><ID>`.
        - **Validare:** Trebuie să conțină cifre. Ignoră codurile de Registrul Comerțului (format J.../.../...).
        - **Normalizare:** Elimină prefixul "RO" din câmpul `vanzator_cui` și `cumparator_cui` (păstrează doar cifrele). Păstrează "RO" doar în câmpurile `*_cui_tva`.
        - **Adresa:** Concatenează inteligent `<StreetName>`, `<CityName>`, `<CountrySubentity>`. Normalizare: "București" -> "Bucuresti".

        ### 2. Detalii Factură
        - **Număr:** Extrage din `<cbc:ID>`.
        - **Sume:** Folosește `<PayableAmount>` pentru total și `<TaxTotal><TaxAmount>` pentru TVA. Asigură-te că sunt `float`.

        ### 3. Logica Critică: Modalitate de Plată
        Verifică tag-ul `<cbc:PaymentMeansCode>`.
        - Dacă tag-ul **LIPSEȘTE** complet -> Setează `modalitate_plata` = "CARD".
        - Dacă valoarea este "10" -> Setează `modalitate_plata` = "NUMERAR".
        - Dacă valoarea este "42", "48", "31", "30" sau orice altceva -> Setează `modalitate_plata` = "CARD".

        ### 4. Procesarea Produselor (Smart Summary)
        XML-ul poate conține mai multe linii `<cac:InvoiceLine>`.
        - Concatenează toate numele produselor sau serviciilor într-un singur string, separate prin virgulă.

        ### 5. Clasificare: Obiect de Inventar
        - `obiect_de_inventar`:
            - Obiect de inventar (True): Doar dacă poți atinge produsul (hardware, mobilier, PC, Laptop, masa, scaun, etc).
            - Servicii/Cloud (False): Orice înseamnă subscripție, hosting, Certificat digital cu USB, VPS (ex: Nanode, instanțe EC2, domenii web) sau consumabile (hartie, produse de curatenie, apa, baterii etc).
            - Dacă un item are o specificație de memorie (ex: 1GB, 512MB) dar nu are o descriere de hardware fizic (PC, Laptop etc.), clasifică-l ca Serviciu/False.

        ## Reguli de Formatare:
        - Toate string-urile trebuie normalizate la ASCII (fără diacritice: ș->s, ț->t).
        - Datele calendaristice: YYYY-MM-DD.
        """
        return prompt

    @retry(
        stop=(stop_after_delay(600) | stop_after_attempt(2)),
        wait=wait_random(min=1, max=5),
    )
    @jitter_call(3, 6)
    def get_llm_response(
        self, xml_efactura_string: str, temperature: float = 0
    ) -> EFacturaSchema:
        response = self.client.models.generate_content(
            model=self.model,
            contents=[xml_efactura_string],
            config=types.GenerateContentConfig(
                system_instruction=self.system_prompt,
                temperature=temperature,
                response_mime_type="application/json",
                response_json_schema=EFacturaSchema.model_json_schema(),
            ),
        )

        response_schema = EFacturaSchema.model_validate_json(
            extract_json_from_llm_response(response.text)
        )

        return response_schema

    def group_pdf_zip_filepaths(self, extracted_pdfs_and_zips: list[str]):
        # Make groups of pdf+zip
        grouped_by_id = {}
        for fp in extracted_pdfs_and_zips:
            name_no_ext = Path(fp).stem
            if name_no_ext not in grouped_by_id:
                grouped_by_id[name_no_ext] = set()
            grouped_by_id[name_no_ext].add(fp)

        pdfzip_groups: list[PDFZIPGroup] = []
        for fid, filepaths in grouped_by_id.items():
            # Group pdf + zip neaparat
            if len(filepaths) < 2:
                continue

            # Possible duplicates keep just 1 pair
            zip_path = None
            for fp in filepaths:
                if fp.endswith(".zip"):
                    zip_path = fp
                    break

            pdf_path = None
            for fp in filepaths:
                if fp.endswith(".pdf"):
                    pdf_path = fp
                    break

            if pdf_path is None or zip_path is None:
                continue

            # Ensure PDF is e-factura
            reader = PdfReader(pdf_path)
            page = reader.pages[0]
            text = page.extract_text()

            if "RO eFactura" not in text:
                continue

            are_cod_tva = False
            if isinstance(self.cod_tva, str) and self.cod_tva:
                are_cod_tva = self.cod_tva in text

            are_cui = False
            if isinstance(self.cui_cif, str) and self.cui_cif:
                are_cui = self.cui_cif in text

            are_date_identificare = are_cod_tva or are_cui

            if not are_date_identificare:
                continue

            # Ensure ZIP is e-factura
            e_factura_xml = False
            semnatura_xml = False
            efactura_xml = None
            with zipfile.ZipFile(zip_path, "r") as z:
                for member in z.infolist():
                    name = member.filename

                    if not e_factura_xml:
                        e_factura_xml_match = re.search(r"^\d{10}\.xml$", name)
                        if e_factura_xml_match:
                            e_factura_xml = True
                            with z.open(member) as f:
                                efactura_xml = f.read()

                    if not semnatura_xml:
                        semnatura_xml_match = re.search(
                            r"^semnatura_\d{10}\.xml$", name
                        )
                        if semnatura_xml_match:
                            semnatura_xml = True

            if e_factura_xml is False or semnatura_xml is False:
                continue

            # Make group
            data = PDFZIPGroup(
                pdf_name=Path(pdf_path).name,
                pdf_path=pdf_path,
                zip_name=Path(zip_path).name,
                zip_path=zip_path,
                efactura_xml=efactura_xml,
            )

            pdfzip_groups.append(data)

        return pdfzip_groups

    def extract_pdf_paths_from_zip(self) -> list[str]:
        """Extract .pdf and .zip files from given zip"""
        zip_path = Path(self.zip_path)

        unique_filenames = set()
        extracted_pdfs_and_zips = []

        def process_zip(path_to_zip: Path):
            """Extract a zip, recursively scan files, process nested zip files."""
            with zipfile.ZipFile(path_to_zip, "r") as z:
                for member in z.infolist():
                    # Extract into memory
                    with z.open(member) as f:
                        data = f.read()

                    name = member.filename
                    name_path = Path(name)

                    pdf_match = re.search(r"\d{10}.pdf", name_path.name)
                    zip_match = re.search(r"\d{10}.zip", name_path.name)

                    # E-Factura PDF
                    if pdf_match and name not in unique_filenames:
                        unique_filenames.add(name_path.name)

                        out_path = Path(self.shared_dir).joinpath(name_path.name)
                        out_path.write_bytes(data)

                        extracted_pdfs_and_zips.append(str(out_path))
                        continue

                    # E-Factura ZIP
                    if zip_match and name not in unique_filenames:
                        unique_filenames.add(name_path.name)

                        out_path = Path(self.shared_dir).joinpath(name_path.name)
                        out_path.write_bytes(data)

                        extracted_pdfs_and_zips.append(str(out_path))
                        continue

                    # Nested zip
                    if name_path.suffix.lower() == ".zip" and not zip_match:
                        nested_zip_temp = Path(self.shared_dir).joinpath(
                            f"nested_{uuid.uuid4()}.zip"
                        )
                        nested_zip_temp.write_bytes(data)

                        # Recurse
                        process_zip(nested_zip_temp)

                    # Ignore all other file types

        # Begin processing the root zip
        process_zip(zip_path)

        return extracted_pdfs_and_zips

    def proceseaza_grup_pdf_zip(self, pzgroup: PDFZIPGroup):
        self.send_message(f"Procesam e-factura {pzgroup.pdf_name}...")

        efactura = self.get_llm_response(pzgroup.efactura_xml)
        efactura.nume_fisier = Path(pzgroup.pdf_name).stem
        efactura.modalitate_plata = ModalitatePlata.CARD

        # NOTE: Am gasit cazuri de plata cu cardul dar e-factura era codul pentru numerar...
        # if "<cbc:PaymentMeansCode>10</cbc:PaymentMeansCode>" in pzgroup.efactura_xml:
        #     efactura.modalitate_plata = ModalitatePlata.NUMERAR
        # else:
        #     efactura.modalitate_plata = ModalitatePlata.CARD

        return PZGroupBatch(pzgroup=pzgroup, efactura=efactura)

    def extrage_date_din_efacturi_colectate(self, pdfzip_groups: list[PDFZIPGroup]):
        rows = []

        BATCH_SIZE = 10
        data_nou_prag_mijloc_fix = datetime.datetime(2026, 2, 26)

        for i in range(0, len(pdfzip_groups), BATCH_SIZE):
            batch = pdfzip_groups[i : i + BATCH_SIZE]

            results: list[PZGroupBatch] = Parallel(
                n_jobs=BATCH_SIZE,
                backend="threading",
            )(delayed(self.proceseaza_grup_pdf_zip)(pz) for pz in batch)

            for result in results:
                pzgroup = result.pzgroup
                efactura = result.efactura

                if efactura.obiect_de_inventar:
                    data_referinta = efactura.data_tranzactie or efactura.data_emitere
                    prag = (
                        5000
                        if data_referinta >= data_nou_prag_mijloc_fix
                        else 2500
                    )

                    if efactura.total_de_plata >= prag:
                        efactura.mijloc_fix = True

                        fmj_path = creeaza_fisa_mijloc_fix(
                            client=self.client,
                            document_path=Path(pzgroup.pdf_path),
                            valoare_produs_ron=efactura.total_de_plata,
                            data_emitere=efactura.data_emitere,
                        )

                        # Move FMJ PDF
                        pdf_src = Path(fmj_path)
                        pdf_dst = self.efacturi_path / pdf_src.name
                        pdf_src.rename(pdf_dst)

                        efactura.nume_fisier_mijloc_fix = pdf_src.name

                # Move PDF
                pdf_src = Path(pzgroup.pdf_path)
                pdf_dst = self.efacturi_path / pdf_src.name
                pdf_src.rename(pdf_dst)

                # Move ZIP
                zip_src = Path(pzgroup.zip_path)
                zip_dst = self.efacturi_path / zip_src.name
                zip_src.rename(zip_dst)

                rows.append(efactura)

        self.send_message("Am terminat de procesat e-facturile!")
        return rows

    def get_consolidated_bank_statements_with_exact_match(
        self,
        extrase_bancare: list[ExtrasBancarLineSchema],
        facturi: list[EFacturaSchema],
    ):
        hashed_transactions = set()

        for factura in facturi:
            posibilitate_match: list[ExtrasBancarLineSchema] = []

            for extras in extrase_bancare:
                if get_model_hash(extras) in hashed_transactions:
                    continue

                if factura.modalitate_plata != ModalitatePlata.CARD:
                    continue

                if extras.valuta_cont != "RON":
                    continue

                if extras.data < factura.data_emitere:
                    continue

                if not any([extras.debit, extras.credit]):
                    continue

                if extras.debit:
                    if extras.debit != factura.total_de_plata:
                        continue

                if extras.credit:
                    if extras.credit != factura.total_de_plata:
                        continue

                posibilitate_match.append(extras)

            # Gaseste cea mai apropiata data
            if not posibilitate_match:
                factura.data_tranzactie = factura.data_emitere
            else:
                closest_match = min(
                    posibilitate_match, key=lambda x: x.data - factura.data_emitere
                )
                factura.data_tranzactie = closest_match.data
                factura.nume_fisier_extras_bancar = closest_match.nume_fisier

                hashed_transactions.add(get_model_hash(closest_match))

        for factura in facturi:
            if not factura.data_tranzactie:
                factura.data_tranzactie = factura.data_emitere

        return facturi

    def consolidated_with_bank_statements(self, facturi: list[EFacturaSchema]):
        if self.zip_path_extrase_bancare is None:
            return facturi

        with zipfile.ZipFile(self.zip_path_extrase_bancare, "r") as zip_ref:
            zip_ref.extractall(self.report_path)

        raport_extrase_bancare_path = self.report_path / "raport_extrase_bancare.xlsx"
        datali = get_data_from_excel(raport_extrase_bancare_path)
        if not datali:
            return facturi

        extrase_bancare = [ExtrasBancarLineSchema(**data) for data in datali]

        facturi = self.get_consolidated_bank_statements_with_exact_match(
            extrase_bancare, facturi
        )

        return facturi

    def process_zip(self):
        try:
            self.send_message("Extragem e-facturile in grupuri de pdf+zip...")

            if self.extracted_pdfs_and_zips is None and self.pdfzip_groups is None:
                extracted_pdfs_and_zips = self.extract_pdf_paths_from_zip()
                pdfzip_groups = self.group_pdf_zip_filepaths(extracted_pdfs_and_zips)
            else:
                extracted_pdfs_and_zips = self.extracted_pdfs_and_zips
                pdfzip_groups = self.pdfzip_groups

            if len(pdfzip_groups) == 0:
                log.info("Nici o e-factura gasita.")
                return

            self.send_message(
                f"Extragem informatiile din {len(pdfzip_groups)} e-facturi colectate..."
            )

            rows = self.extrage_date_din_efacturi_colectate(pdfzip_groups)
            rows = self.consolidated_with_bank_statements(rows)

            # Sort from newest to oldest by date
            total_efacturi = len(rows)

            self.send_message(
                f"Am colectat {total_efacturi} e-facturi. Cream raportul final."
            )

            rows = sorted(rows, key=lambda r: r.data_emitere, reverse=True)
            rows_dict = [r.model_dump() for r in rows]

            # Create report
            create_excel(
                rows_dict,
                output_path=f"{self.report_path}/raport_efactura.xlsx",
            )

            # Make ZIP report
            report_dir = Path(self.report_path)
            shared_dir = Path(self.shared_dir)

            zip_name = f"raport_efacturi_{uuid.uuid4().hex}.zip"
            zip_path = shared_dir / zip_name

            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
                for file_path in report_dir.rglob("*"):
                    if file_path.is_file():
                        # keep paths relative to report_dir
                        z.write(file_path, arcname=file_path.relative_to(report_dir))

            return RaspunsRaport(
                zip_path=str(zip_path),
                total_documente=total_efacturi,
            )
        except Exception as ex:
            log.exception(ex)
            raise

    def cleanup(self):
        safe_rmtree(self.shared_dir)
