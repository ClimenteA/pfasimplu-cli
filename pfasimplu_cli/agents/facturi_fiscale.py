import datetime
import multiprocessing
import os
import re
import uuid
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

from cursvalutarbnr import Currency, ron_exchange_rate
from google import genai
from google.genai import types
from joblib import Parallel, delayed
from loguru import logger as log
from pydantic import BaseModel, Field, model_validator
from tenacity import retry, stop_after_attempt, stop_after_delay, wait_random
from unidecode import unidecode

from pfasimplu_cli.ocr import ExtractedTextResponse, extract_text
from pfasimplu_cli.utils import (
    create_excel,
    extract_json_from_llm_response,
    get_mime_type_from_file,
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


class FacturaFiscalaSchema(BaseModel):
    # Sha256 ID
    nume_fisier: Optional[str] = Field(
        default=None, description="Nu completa acest camp."
    )
    # Date Factura
    numar_factura: str = Field(
        description="Numarul de identificare al facturii. Apare in pagina inainte de listarea detaliilor vanzatorului si cumparatorului."
    )
    data_emitere: date = Field(
        description="Data emiterii facturii (Data emiterii, Issue Date, Invoice date, From  etc.)"
    )
    data_scadenta: date = Field(
        description="Data scadenta facturii (Data scadenta, Due Date, Invoice to be paid until, To etc.)"
    )
    valuta: Currency = Field(
        description="Cod valuta ISO 4217. Returneaza DOAR una dintre valorile permise. Daca gasesti LEI returneaza RON"
    )
    total_de_plata: float = Field(description="Total de plata")
    total_de_plata_in_ron: Optional[float] = Field(
        default=0, description="Nu completa acest camp."
    )
    valoare_tva: float = Field(
        description="Total de plata TVA. Suma TVA din acea factura."
    )
    valoare_tva_in_ron: Optional[float] = Field(
        default=0, description="Nu completa acest camp."
    )
    modalitate_plata: ModalitatePlata = Field(
        default=ModalitatePlata.CARD,
        description="Modalitatea de plata folosita 'NUMERAR' (bani cash in mana) sau 'CARD' (bancar).",
    )
    # Date vanzator
    vanzator_nume: str = Field(
        description="Numele vanzatorului/firmei care vinde acele produse sau servicii. Atentie nu include si label-ul ex: 'Furnizor: Costel SRL' va fi extras > 'Costel SRL'"
    )
    vanzator_cui: str = Field(
        description="CUI-ul vanzatorului de forma RO14399840 sau 14399840. Uneori numarul dupa Tax ID."
    )
    vanzator_cui_tva: Optional[str] = Field(
        default=None,
        description="Cod TVA al vanzatorului (daca exista). Uneori numarul dupa Tax ID",
    )
    vanzator_adresa: str = Field(description="Adresa vanzatorului")
    # Date comparator
    cumparator_nume: str = Field(
        description="Numele cumparatorului. Atentie nu include si label-ul ex: 'Furnizor: Costel SRL' va fi extras > 'Costel SRL'"
    )
    cumparator_cui: Optional[str] = Field(
        default=None,
        description="CUI-ul cumparatorului de forma RO14399840, 14399840 sau 6487799. Uneori numarul dupa Tax ID.",
    )
    cumparator_cui_tva: Optional[str] = Field(
        default=None,
        description="CUI-ul cumparatorului de forma RO14399840. Uneori numarul dupa Tax ID. ",
    )
    cumparator_adresa: str = Field(description="Adresa cumparatorului")
    # Date produse servicii
    produse_sau_servicii: str = Field(
        description="O descriere scurta a serviciilor sau produselor achizitionate (pot fi mai multe linii) ex: 'Lenovo Mouse Lenovo Essential, USB, 1600DPI, Negru / Rosu' > 'Lenovo Mouse Lenovo'"
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

        self.numar_factura = self.numar_factura.lstrip("0")

        if self.data_emitere > self.data_scadenta:
            self.data_scadenta = self.data_emitere

        if self.valuta != "RON":
            if self.total_de_plata and not self.total_de_plata_in_ron:
                self.total_de_plata_in_ron = ron_exchange_rate(
                    amount=self.total_de_plata,
                    currency=self.valuta,
                    date=self.data_emitere,
                )
            if self.valoare_tva and not self.valoare_tva_in_ron:
                self.valoare_tva_in_ron = ron_exchange_rate(
                    amount=self.valoare_tva,
                    currency=self.valuta,
                    date=self.data_emitere,
                )
        else:
            self.total_de_plata_in_ron = self.total_de_plata
            self.valoare_tva_in_ron = self.valoare_tva

        return self


class FacturaBatch(BaseModel):
    factura_fiscala: FacturaFiscalaSchema


@dataclass
class AgentFacturiFiscale:
    zip_path: str
    cui_cif: str
    cod_tva: str
    zip_path_extrase_bancare: str | None = None
    model: str = "gemini-3.8-flash"
    client: genai.Client | None = None
    api_key: str | None = None

    def __post_init__(self):
        self.filepaths = None
        self.facturi_fiscale = None

        if self.client is None:
            if not self.api_key:
                raise ValueError(
                    "AgentFacturiFiscale requires a Gemini client or api_key"
                )
            self.client = genai.Client(api_key=self.api_key)
        self.shared_dir = str(make_workdir("pfasimplu_facturi_"))
        self.facturi_fiscale_path = (
            Path(self.shared_dir) / "facturi_fiscale_colectate" / "facturi_fiscale"
        )
        self.report_path = Path(self.shared_dir) / "facturi_fiscale_colectate"
        self.facturi_fiscale_path.mkdir(parents=True, exist_ok=True)

    def send_message(self, message: str):
        log.info(f"Facturi fiscale: {message}")

    @property
    def system_prompt(self):
        prompt = """
        ## ROL
        Ești un sistem OCR inteligent specializat în procesarea facturilor fiscale.
        Sarcina ta este să analizezi factura fiscală primită și să extragi datele strict conform schemei Pydantic 'FacturaFiscalaSchema'.

        ## INSTRUCȚIUNI DE NAVIGARE ȘI LOGICĂ
        1. **Identificare Părți**: 
        - Vanzator (Supplier/Furnizor): De obicei situat în partea de sus sau stânga sus. Conține datele entității care emite factura.
        - Cumparator (Client/Beneficiar): Entitatea către care se facturează. Atenție la persoanele fizice (nu vor avea CUI).
        2. **Date și Valori**:
        - Folosește formatul ISO (YYYY-MM-DD) pentru toate câmpurile de tip `date`.
        - Pentru `float` (sume), folosește punctul `.` ca separator zecimal și elimină orice separator de mii (ex: "1.250,50" devine 1250.50).
        3. **Multilingvism**:
        - Dacă factura este în altă limbă decât Româna, extrage textul original și adaugă traducerea in româna în câmpul `produse_sau_servicii`.

        ## REGULI STRICTE
        - **Fără Fabricații**: Dacă un câmp nu există în document, returnează `null` (pentru Optional) sau un string gol, nu inventa date.
        - **Output**: Returnează DOAR codul JSON valid, fără explicații suplimentare înainte sau după blocul de cod.
        - **IGNORA CAMPURILE**: `nume_fisier`, `valoare_tva_in_ron`, `total_de_plata_in_ron` (lasa-le null).

        ## DETALII SPECIFICE CÂMPURI
        - Pentru campurile `vanzator_cui`, `cumparator_cui`: Extrage doar cifrele (și prefixul RO dacă există). Dacă apare "Tax ID", "VAT", "CIF", "CUI" acela este CUI-ul. 
            - ATENTIE: VANZATORUL/SUPPLIER/FURNIZORUL ARE INTOTDEAUNA CUI! 
            - ATENTIE: CUMPARATORUL ARE INTOTDEAUNA CUI DACA NU ESTE DOAR UN NUME PERSONAL (ex: Georgescu Ion - fara sa aiba PFA sau PERSOANA FIZICA AUTORIZATA sau SRL, LTD etc). 
                - EX: DACA CONTINE PERSOANA FIZICA AUTORIZATA, PFA, SRL, LTD ETC. ORICE IDENTIFICATOR DIN CARE PUTEM DEDUCE CA ESTE O FIRMA ATUNCI ARE SI CUI/TAX ID ETC.
        - `produse_sau_servicii`: 
            - Concatenează toate numele produselor sau serviciilor într-un singur string, separate prin virgulă.
            - Ignora disconturile (sumele cu minus) ex: "Discount conform promotie: eMAG-920-007931 [DPRO1VAT20TYPE11]buc1 | -40,34 | 19 | -40,34 | -7,66"
        - `modalitate_plata`: 
            - Plata poate fi NUMERAR (bani in mana, ramburs numerar) sau CARD (prin transfer bancar). 
            - ATENTIE: DACA NU gasesti cuvinte/keywords care sa indice ca plata a fost facuta NUMERAR nu completa acest camp. 
                Ex: "Plata numerar/POS ramburs, Plata numerar/POS cu bon fiscal/chitanta" - se mentioneaza ambele "numerar/POS" - aici va fi default CARD.
        - `obiect_de_inventar`:
            - Obiect de inventar (True): Doar dacă poți atinge produsul (hardware, mobilier, PC, Laptop, masa, scaun, etc).
            - Servicii/Cloud (False): Orice înseamnă subscripție, hosting, VPS (ex: Nanode, instanțe EC2, domenii web) sau consumabile (hartie, produse de curatenie, apa, baterii etc).
            - Dacă un item are o specificație de memorie (ex: 1GB, 512MB) dar nu are o descriere de hardware fizic (PC, Laptop etc.), clasifică-l ca Serviciu/False.
        - `numar_factura`: La facturile in romana apar de obicei in forma Serie XX Numar XX - extrage valoarea pentru campul `numar_factura` FARA SERIE.

        """
        return prompt

    @retry(
        stop=(stop_after_delay(600) | stop_after_attempt(2)),
        wait=wait_random(min=1, max=5),
    )
    @jitter_call(3, 6)
    def get_llm_response(
        self,
        factura_path: str,
        mime_type: str = "application/pdf",
        temperature: float = 0,
    ) -> FacturaFiscalaSchema:
        response = self.client.models.generate_content(
            model=self.model,
            contents=[
                types.Part.from_bytes(
                    data=Path(factura_path).read_bytes(),
                    mime_type=mime_type,
                ),
                "Extrage corect datele din acesta factura fiscala.",
            ],
            config=types.GenerateContentConfig(
                system_instruction=self.system_prompt,
                temperature=temperature,
                response_mime_type="application/json",
                response_json_schema=FacturaFiscalaSchema.model_json_schema(),
            ),
        )

        response_schema = FacturaFiscalaSchema.model_validate_json(
            extract_json_from_llm_response(response.text)
        )

        return response_schema

    def extrage_imagini_si_pdf_din_zip(self) -> list[str]:
        zip_path = Path(self.zip_path)

        extracted_paths = []

        def process_zip(path_to_zip: Path):
            """Extract a zip, recursively scan files, process nested zip files."""
            with zipfile.ZipFile(path_to_zip, "r") as z:
                for member in z.infolist():
                    # Extract into memory
                    with z.open(member) as f:
                        data = f.read()

                    name = member.filename
                    name_path = Path(name)

                    img_or_pdf_match = re.search(
                        r"\.(jpg|jpeg|png|pdf)$", name_path.name, re.IGNORECASE
                    )

                    # Poza sau pdf cu factura (probabil)
                    if img_or_pdf_match:
                        out_filename = (
                            f"{uuid.uuid4().hex[:16]}{name_path.suffix.lower()}"
                        )

                        if "storno" in name_path.name.lower():
                            out_filename = "storno_" + out_filename

                        out_path = Path(self.shared_dir).joinpath(out_filename)

                        out_path.write_bytes(data)

                        extracted_paths.append(str(out_path))
                        continue

                    # Nested zip
                    if name_path.suffix.lower() == ".zip":
                        nested_zip_temp = Path(self.shared_dir).joinpath(
                            f"nested_{uuid.uuid4()}.zip"
                        )
                        nested_zip_temp.write_bytes(data)

                        # Recurse
                        process_zip(nested_zip_temp)

                    # Ignore all other file types

        # Begin processing the root zip
        process_zip(zip_path)

        return extracted_paths

    def extract_text_wrapper(self, filepath) -> tuple[str, ExtractedTextResponse]:
        return filepath, extract_text(
            filepath, client=self.client, model=self.model
        )

    def preia_doar_facturi_normale(
        self, filepaths: list[str]
    ) -> list[ExtractedTextResponse]:
        facturi_unice: set[str] = set()
        facturi_fiscale: list[ExtractedTextResponse] = []

        # Parallel calls (threaded)
        results: list[tuple[str, ExtractedTextResponse]] = Parallel(
            n_jobs=multiprocessing.cpu_count(),
            backend="threading",
            prefer="threads",
            batch_size=1,
        )(delayed(self.extract_text_wrapper)(fpath) for fpath in filepaths)

        # Serial filtering + logging
        for filepath, response in results:
            self.send_message(f"Verificam daca {Path(filepath).name} este o factura...")

            if response is None or response.error:
                self.send_message(f"Nu am putut analiza fisierul {filepath}")
                continue

            text = unidecode(response.text.lower())

            has_cui_number = re.search(r"\d{8}", text)
            has_invoice_text = any([i in text for i in ["invoice", "factura"]])
            has_total = "total" in text or "impreuna" in text
            nu_e_un_extras_bancar = not all(
                [i in text for i in ["debit", "credit", "balance", "statement"]]
            )
            nu_e_efactura = not all(
                [
                    i in text
                    for i in [
                        "ro efactura",
                        "vanzator",
                        "cumparator",
                        "pagina",
                        "rotunjire",
                        "moneda",
                    ]
                ]
            )
            nu_e_cerere_caen_rev3 = (
                "actualizare obiect de activitate conform caen rev.3" not in text
            )

            are_cod_tva = False
            if isinstance(self.cod_tva, str) and self.cod_tva:
                are_cod_tva = self.cod_tva in text

            are_cui = False
            if isinstance(self.cui_cif, str) and self.cui_cif:
                are_cui = self.cui_cif in text

            are_date_identificare = are_cod_tva or are_cui

            if all(
                [
                    has_cui_number,
                    has_invoice_text,
                    has_total,
                    nu_e_un_extras_bancar,
                    nu_e_efactura,
                    nu_e_cerere_caen_rev3,
                    are_date_identificare,
                ]
            ):
                if response.text_hash in facturi_unice:
                    self.send_message(
                        f"Factura duplicat ignorata: {Path(filepath).name}"
                    )
                    continue

                src = Path(filepath)
                filename = f"factura_{src.name}"
                dst = self.facturi_fiscale_path / filename
                src.rename(dst)

                response.filepath = str(dst)

                self.send_message(f"Este o factura {filename}")
                facturi_unice.add(response.text_hash)
                facturi_fiscale.append(response)

        return facturi_fiscale

    def proceseaza_factura_colectata(self, factura_colectata: ExtractedTextResponse):
        self.send_message(f"Procesam factura {factura_colectata.text_hash}...")

        mime_type = get_mime_type_from_file(factura_colectata.filepath)

        factura = self.get_llm_response(
            factura_colectata.filepath, mime_type=mime_type
        )
        factura.nume_fisier = factura_colectata.filepath

        if "storno_" in Path(factura_colectata.filepath).stem:
            factura.total_de_plata = 0
            factura.total_de_plata_in_ron = 0
            factura.valoare_tva = 0
            factura.valoare_tva_in_ron = 0
            factura.produse_sau_servicii = "STORNO - factura neincasata"

        return FacturaBatch(factura_fiscala=factura)

    def extrage_date_din_facturile_colectate(
        self, facturi_fiscale: list[ExtractedTextResponse]
    ) -> list[FacturaFiscalaSchema]:
        rows: list[FacturaFiscalaSchema] = []

        BATCH_SIZE = 10

        data_nou_prag_mijloc_fix = datetime.datetime(2026, 2, 26)

        for i in range(0, len(facturi_fiscale), BATCH_SIZE):
            batch = facturi_fiscale[i : i + BATCH_SIZE]

            results: list[FacturaBatch] = Parallel(
                n_jobs=BATCH_SIZE,
                backend="threading",
            )(delayed(self.proceseaza_factura_colectata)(b) for b in batch)

            for result in results:
                factura = result.factura_fiscala

                # Move file
                src = Path(factura.nume_fisier)
                nume_fisier = src.name
                dst = self.facturi_fiscale_path / nume_fisier
                src.rename(dst)

                factura.nume_fisier = nume_fisier

                if factura.obiect_de_inventar:
                    data_referinta = factura.data_tranzactie or factura.data_emitere
                    prag = (
                        5000
                        if data_referinta >= data_nou_prag_mijloc_fix
                        else 2500
                    )

                    if factura.total_de_plata_in_ron >= prag:
                        factura.mijloc_fix = True

                        fmj_path = creeaza_fisa_mijloc_fix(
                            client=self.client,
                            document_path=dst,
                            valoare_produs_ron=factura.total_de_plata_in_ron,
                            data_emitere=factura.data_emitere,
                        )

                        # Move FMJ PDF
                        pdf_src = Path(fmj_path)
                        pdf_dst = self.facturi_fiscale_path / pdf_src.name
                        pdf_src.rename(pdf_dst)

                        factura.nume_fisier_mijloc_fix = pdf_src.name

                rows.append(factura)

        self.send_message("Am terminat de procesat facturile fiscale!")
        return rows

    def elimina_facturile_duplicate(self, facturi: list[FacturaFiscalaSchema]):
        to_delete = []
        unique_map = {}
        # Keep only unique, gather to delete files
        for fact in facturi:
            key = (
                fact.numar_factura,
                fact.vanzator_cui,
                fact.cumparator_cui,
                fact.total_de_plata,
            )

            if key not in unique_map:
                unique_map[key] = fact
            else:
                if fact.data_emitere > unique_map[key].data_emitere:
                    to_delete.append(unique_map[key].nume_fisier)
                    unique_map[key] = fact
                else:
                    to_delete.append(fact.nume_fisier)

        # Delete duplicate files from disk
        for filename in to_delete:
            file_path = self.facturi_fiscale_path / filename
            os.remove(file_path)

        return list(unique_map.values())

    def get_consolidated_bank_statements_with_exact_match(
        self,
        extrase_bancare: list[ExtrasBancarLineSchema],
        facturi: list[FacturaFiscalaSchema],
    ):
        hashed_transactions = set()

        for factura in facturi:
            posibilitate_match: list[ExtrasBancarLineSchema] = []

            for extras in extrase_bancare:
                if get_model_hash(extras) in hashed_transactions:
                    continue

                if factura.modalitate_plata != ModalitatePlata.CARD:
                    continue

                if extras.valuta_cont != factura.valuta:
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

                if factura.valuta != "RON":
                    factura.total_de_plata_in_ron = ron_exchange_rate(
                        amount=factura.total_de_plata,
                        currency=factura.valuta,
                        date=factura.data_tranzactie,
                    )

                    if factura.valoare_tva:
                        factura.valoare_tva_in_ron = ron_exchange_rate(
                            amount=factura.valoare_tva,
                            currency=factura.valuta,
                            date=factura.data_tranzactie,
                        )

        for factura in facturi:
            if not factura.data_tranzactie:
                factura.data_tranzactie = factura.data_emitere

        return facturi

    def consolidated_with_bank_statements(self, facturi: list[FacturaFiscalaSchema]):
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
            self.send_message(
                "Extragem facturile fiscale din format .pdf, .jpg, .jpeg, .png ..."
            )

            if self.filepaths is None and self.facturi_fiscale is None:
                filepaths = self.extrage_imagini_si_pdf_din_zip()
                facturi_fiscale = self.preia_doar_facturi_normale(filepaths)
            else:
                filepaths = self.filepaths
                facturi_fiscale = self.facturi_fiscale

            if len(facturi_fiscale) == 0:
                log.info("Nici o factura fiscala gasita.")
                return

            self.send_message(
                f"Extragem informatiile din {len(facturi_fiscale)} facturi colectate..."
            )

            rows = self.extrage_date_din_facturile_colectate(facturi_fiscale)
            rows = self.elimina_facturile_duplicate(rows)
            rows = self.consolidated_with_bank_statements(rows)

            # Sort from newest to oldest by date
            total_facturi_fiscale = len(rows)

            self.send_message(
                f"Am colectat {total_facturi_fiscale} facturi fiscale. Cream raportul final."
            )

            rows = sorted(rows, key=lambda r: r.data_emitere, reverse=True)
            rows_dict = [r.model_dump() for r in rows]

            # Create reports in different formats and add it to the zip of processed pdfs
            create_excel(
                rows_dict,
                output_path=f"{self.report_path}/raport_facturi_fiscale.xlsx",
            )

            # Make ZIP report
            report_dir = Path(self.report_path)
            shared_dir = Path(self.shared_dir)

            zip_name = f"raport_facturi_{uuid.uuid4().hex}.zip"
            zip_path = shared_dir / zip_name

            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
                for file_path in report_dir.rglob("*"):
                    if file_path.is_file():
                        # keep paths relative to report_dir
                        z.write(file_path, arcname=file_path.relative_to(report_dir))

            return RaspunsRaport(
                zip_path=str(zip_path),
                total_documente=total_facturi_fiscale,
            )

        except Exception as ex:
            log.exception(ex)
            return

    def cleanup(self):
        safe_rmtree(self.shared_dir)
