import datetime
import multiprocessing
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
from pydantic import BaseModel, Field
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


class BonFiscalSchema(BaseModel):
    nume_fisier: str = Field(default=None, description="Nu completa acest camp.")
    vanzator_nume: str = Field(
        description="Numele firmei vanzatorului (PFA sau SRL) apare in prima parte a textului"
    )
    vanzator_cui: Optional[str] = Field(
        default=None,
        description="CUI-ul vanzatorului apare sub forma de CIF/C.I.F.: 12345678 sau CUI: 12345678 (8 cifre). Poate avea un RO ca prefix la numar.",
    )
    cumparator_cui: Optional[str] = Field(
        default=None,
        description="CUI/C.U.I/CIF/C.I.F. cumparatorului/clientului daca este disponibil. CUI/CIF: 12345678 are 8 cifre si poate avea RO ca prefix.",
    )
    total_de_plata: float = Field(
        description="Total de plata din bonul fiscal apare dupa detalii servicii si inainte de detalierea datei la care s-a facut tranzactia. De obicei apare in forma TOTAL: 23,45 (un numar)."
    )
    valoare_tva: Optional[float] = Field(
        default=0,
        description="Daca gasesti pe bon 'NEPLATITOR TVA' NU completa acest camp. Daca nu ia valoarea in ron/lei din dreptul 'TOTAL TVA'.",
    )
    data: date = Field(
        description="Data la care a fost efectuata tranzactia. Apare inainte de textul 'BON FISCAL' in formatul zi-luna-an ex: 30-06-22 sau 23-01-2025. Returneaza data in isoformat YYYY-MM-DD ex: 30-06-22 > 2022-06-30, 23-01-2025 > 2025-01-23"
    )
    modalitate_plata: ModalitatePlata = Field(
        description="Cum a fost platit: NUMERAR sau CARD. Apare de obicei dupa totalul de plata."
    )
    produse_sau_servicii: str = Field(
        description="Analizeaza tot bonul si fa o lista cu ce a fost achizitionat. Ex: Transport taxi sau Produsul 1, 2 3 etc."
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


class BonFiscalBatch(BaseModel):
    bonfiscal: BonFiscalSchema


@dataclass
class AgentBonuriFiscale:
    zip_path: str
    cui_cif: str
    cod_tva: str
    zip_path_extrase_bancare: str | None = None
    model: str = "gemini-3.8-flash"
    client: genai.Client | None = None
    api_key: str | None = None

    def __post_init__(self):
        self.bonuri_fiscale = None
        self.extracted_image_paths = None
        if self.client is None:
            if not self.api_key:
                raise ValueError("AgentBonuriFiscale requires a Gemini client or api_key")
            self.client = genai.Client(api_key=self.api_key)
        self.shared_dir = str(make_workdir("pfasimplu_bonuri_"))
        self.bonuri_fiscale_path = (
            Path(self.shared_dir) / "bonuri_fiscale_colectate" / "bonuri_fiscale"
        )
        self.report_path = Path(self.shared_dir) / "bonuri_fiscale_colectate"
        self.bonuri_fiscale_path.mkdir(parents=True, exist_ok=True)

    def send_message(self, message: str):
        log.info(f"Bonuri fiscale: {message}")

    @property
    def system_prompt(self):
        prompt = """
        ## ROL
        
        - Esti un expert in colectarea datelor din bonurile fiscale din Romania.
        - Rolul tau este sa extragi datele din imaginea primita, toate datele cerute conform schemei 'BonFiscalSchema' (pydantic).
        
        # IMPORTANT
        
        - NU INVENTA NIMIC, DOAR EXTRAGE DATELE CONFORM SCHEMEI BonFiscalSchema

        # OBSERVATII
        - Pentru campul: `produse_sau_servicii` 
            - Concatenează toate numele produselor sau serviciilor într-un singur string, separate prin virgulă.
        - Pentru campul `obiect_de_inventar`:
            - Obiect de inventar (True): Doar dacă poți atinge produsul (hardware, mobilier, PC, Laptop, masa, scaun, etc).
            - Servicii/Cloud (False): Orice înseamnă subscripție, hosting, Certificat digital cu USB, VPS (ex: Nanode, instanțe EC2, domenii web) sau consumabile (hartie, produse de curatenie, apa, baterii etc).
            - Dacă un item are o specificație de memorie (ex: 1GB, 512MB) dar nu are o descriere de hardware fizic (PC, Laptop etc.), clasifică-l ca Serviciu/False.
        """
        return prompt

    @retry(
        stop=(stop_after_delay(600) | stop_after_attempt(2)),
        wait=wait_random(min=1, max=5),
    )
    @jitter_call(3, 6)
    def get_llm_response(self, filepath: str, temperature: float = 0):
        response = self.client.models.generate_content(
            model=self.model,
            contents=[
                types.Part.from_bytes(
                    data=Path(filepath).read_bytes(),
                    mime_type=get_mime_type_from_file(filepath, "image/jpeg"),
                ),
                "Extrage datele din textul bonului fiscal primit conform schemei BonFiscalSchema",
            ],
            config=types.GenerateContentConfig(
                system_instruction=self.system_prompt,
                temperature=temperature,
                response_mime_type="application/json",
                response_json_schema=BonFiscalSchema.model_json_schema(),
            ),
        )

        response_schema = BonFiscalSchema.model_validate_json(
            extract_json_from_llm_response(response.text)
        )

        return response_schema

    def extrage_imagini_din_zip(self) -> list[str]:
        zip_path = Path(self.zip_path)

        extracted_image_paths = []

        def process_zip(path_to_zip: Path):
            """Extract a zip, recursively scan files, process nested zip files."""
            with zipfile.ZipFile(path_to_zip, "r") as z:
                for member in z.infolist():
                    # Extract into memory
                    with z.open(member) as f:
                        data = f.read()

                    name = member.filename
                    name_path = Path(name)

                    image_match = re.search(
                        r"\.(jpg|jpeg|png)$", name_path.name, re.IGNORECASE
                    )

                    # Imagine bon fiscal (probabil)
                    if image_match:
                        out_path = Path(self.shared_dir).joinpath(
                            uuid.uuid4().hex[:16] + name_path.suffix.lower()
                        )
                        out_path.write_bytes(data)

                        extracted_image_paths.append(str(out_path))
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

        return extracted_image_paths

    def proceseaza_bon_fiscal(self, bonocr: ExtractedTextResponse):
        self.send_message(f"Procesam bonul fiscal {bonocr.text_hash}...")
        bonfiscal = self.get_llm_response(bonocr.filepath)
        bonfiscal.nume_fisier = bonocr.filepath
        return BonFiscalBatch(bonfiscal=bonfiscal)

    def extrage_date_din_bonurile_colectate(
        self, bonuri_fiscale: list[ExtractedTextResponse]
    ):
        rows: list[BonFiscalSchema] = []

        BATCH_SIZE = 10

        data_nou_prag_mijloc_fix = datetime.datetime(2026, 2, 26)

        for i in range(0, len(bonuri_fiscale), BATCH_SIZE):
            batch = bonuri_fiscale[i : i + BATCH_SIZE]

            results: list[BonFiscalBatch] = Parallel(
                n_jobs=BATCH_SIZE,
                backend="threading",
            )(delayed(self.proceseaza_bon_fiscal)(b) for b in batch)

            for result in results:
                bon = result.bonfiscal

                # Move IMG
                src = Path(bon.nume_fisier)
                filename = f"bon_fiscal_{src.name}"
                dst = self.bonuri_fiscale_path / filename
                src.rename(dst)

                if bon.obiect_de_inventar:
                    data_referinta = bon.data_tranzactie or bon.data
                    prag = (
                        5000
                        if data_referinta >= data_nou_prag_mijloc_fix
                        else 2500
                    )

                    if bon.total_de_plata >= prag:
                        bon.mijloc_fix = True

                        fmj_path = creeaza_fisa_mijloc_fix(
                            client=self.client,
                            document_path=dst,
                            valoare_produs_ron=bon.total_de_plata,
                            data_emitere=bon.data,
                        )

                        # Move FMJ PDF
                        pdf_src = Path(fmj_path)
                        pdf_dst = self.bonuri_fiscale_path / pdf_src.name
                        pdf_src.rename(pdf_dst)

                bon.nume_fisier = filename

                rows.append(bon)

        self.send_message("Am terminat de procesat bonurile fiscale!")
        return rows

    def extract_text_wrapper(self, imgpath):
        return imgpath, extract_text(imgpath, client=self.client, model=self.model)

    def preia_doar_bonuri_fiscale(
        self, image_paths: list[str]
    ) -> list[ExtractedTextResponse]:
        bonuri_unice: set[str] = set()
        bonuri_fiscale: list[ExtractedTextResponse] = []

        # Parallel Google OCR calls (threaded)
        results = Parallel(
            n_jobs=multiprocessing.cpu_count(),
            backend="threading",
            prefer="threads",
            batch_size=1,
        )(delayed(self.extract_text_wrapper)(imgpath) for imgpath in image_paths)

        # Serial filtering + logging
        for imgpath, response in results:
            self.send_message(
                f"Verificam daca {Path(imgpath).name} este un bon fiscal..."
            )

            if response is None or response.error:
                self.send_message(f"Nu am putut analiza imaginea {imgpath}")
                continue

            if response.text_hash in bonuri_unice:
                self.send_message(f"Bon fiscal duplicat ignorat: {Path(imgpath).name}")
                continue

            text_upper = unidecode(response.text).upper()

            nu_e_factura = "FACTURA" not in text_upper
            e_un_bon_fiscal = "BON" in text_upper and "FISCAL" in text_upper

            are_cod_tva = False
            if isinstance(self.cod_tva, str) and self.cod_tva:
                are_cod_tva = self.cod_tva in text_upper

            are_cui = False
            if isinstance(self.cui_cif, str) and self.cui_cif:
                are_cui = self.cui_cif in text_upper

            are_date_identificare = are_cod_tva or are_cui

            if all([nu_e_factura, e_un_bon_fiscal, are_date_identificare]):
                self.send_message(f"Este un bon fiscal {Path(imgpath).name}")
                bonuri_unice.add(response.text_hash)
                bonuri_fiscale.append(response)

        return bonuri_fiscale

    def get_consolidated_bank_statements_with_exact_match(
        self,
        extrase_bancare: list[ExtrasBancarLineSchema],
        bonuri: list[BonFiscalSchema],
    ):
        hashed_transactions = set()

        for bon in bonuri:
            posibilitate_match: list[ExtrasBancarLineSchema] = []

            for extras in extrase_bancare:
                if get_model_hash(extras) in hashed_transactions:
                    continue

                if bon.modalitate_plata != ModalitatePlata.CARD:
                    continue

                if extras.valuta_cont != "RON":
                    continue

                if extras.data < bon.data:
                    continue

                if not any([extras.debit, extras.credit]):
                    continue

                if extras.debit:
                    if extras.debit != bon.total_de_plata:
                        continue

                if extras.credit:
                    if extras.credit != bon.total_de_plata:
                        continue

                posibilitate_match.append(extras)

            # Gaseste cea mai apropiata data
            if not posibilitate_match:
                bon.data_tranzactie = bon.data
            else:
                closest_match = min(posibilitate_match, key=lambda x: x.data - bon.data)
                bon.data_tranzactie = closest_match.data
                bon.nume_fisier_extras_bancar = closest_match.nume_fisier

                hashed_transactions.add(get_model_hash(closest_match))

        for bon in bonuri:
            if not bon.data_tranzactie:
                bon.data_tranzactie = bon.data

        return bonuri

    def consolidated_with_bank_statements(self, bonuri: list[BonFiscalSchema]):
        if self.zip_path_extrase_bancare is None:
            return bonuri

        with zipfile.ZipFile(self.zip_path_extrase_bancare, "r") as zip_ref:
            zip_ref.extractall(self.report_path)

        raport_extrase_bancare_path = self.report_path / "raport_extrase_bancare.xlsx"
        datali = get_data_from_excel(raport_extrase_bancare_path)
        if not datali:
            return bonuri

        extrase_bancare = [ExtrasBancarLineSchema(**data) for data in datali]

        bonuri = self.get_consolidated_bank_statements_with_exact_match(
            extrase_bancare, bonuri
        )

        return bonuri

    def process_zip(self):
        try:
            self.send_message(
                "Extragem bonurile fiscale din format .jpg, .jpeg, .png..."
            )

            if self.extracted_image_paths is None and self.bonuri_fiscale is None:
                extracted_image_paths = self.extrage_imagini_din_zip()
                bonuri_fiscale = self.preia_doar_bonuri_fiscale(extracted_image_paths)
            else:
                extracted_image_paths = self.extracted_image_paths
                bonuri_fiscale = self.bonuri_fiscale

            if len(bonuri_fiscale) == 0:
                log.info("Nici un bon fiscal gasit.")
                return

            self.send_message(
                f"Extragem informatiile din {len(extracted_image_paths)} bonuri colectate..."
            )

            rows = self.extrage_date_din_bonurile_colectate(bonuri_fiscale)
            rows = self.consolidated_with_bank_statements(rows)

            # Sort from newest to oldest by date
            total_bonuri_fiscale = len(rows)

            self.send_message(
                f"Am colectat {total_bonuri_fiscale} bonuri fiscale. Cream raportul final."
            )

            rows = sorted(rows, key=lambda r: r.data, reverse=True)
            rows_dict = [r.model_dump() for r in rows]

            # Create report
            create_excel(
                rows_dict,
                output_path=f"{self.report_path}/raport_bonuri_fiscale.xlsx",
            )

            # Make ZIP report
            report_dir = Path(self.report_path)
            shared_dir = Path(self.shared_dir)

            zip_name = f"raport_bonuri_fiscale_{uuid.uuid4().hex}.zip"
            zip_path = shared_dir / zip_name

            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
                for file_path in report_dir.rglob("*"):
                    if file_path.is_file():
                        # keep paths relative to report_dir
                        z.write(file_path, arcname=file_path.relative_to(report_dir))

            return RaspunsRaport(
                zip_path=str(zip_path),
                total_documente=total_bonuri_fiscale,
            )
        except Exception as ex:
            log.exception(ex)
            return

    def cleanup(self):
        safe_rmtree(self.shared_dir)
