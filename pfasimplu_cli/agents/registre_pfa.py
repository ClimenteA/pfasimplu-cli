import glob
import os
import re
import shutil
import statistics
import uuid
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

from google import genai
from joblib import Parallel, delayed
from loguru import logger as log
from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from pfasimplu_cli.utils import create_excel, make_workdir, safe_rmtree

from .bonuri_fiscale import AgentBonuriFiscale, BonFiscalSchema
from .common import ModalitatePlata, RaspunsRaport, get_data_from_excel
from .efacturi import AgentEFacturi, EFacturaSchema
from .extrase_bancare import AgentExtraseBancare, ExtrasBancarLineSchema
from .facturi_fiscale import AgentFacturiFiscale, FacturaFiscalaSchema


class RaspunsProcesareDocumente(BaseModel):
    bonuri: Optional[RaspunsRaport]
    efacturi: Optional[RaspunsRaport]
    extrase_bancare: Optional[RaspunsRaport]
    facturi: Optional[RaspunsRaport]


class ReportsData(BaseModel):
    bonuri: Optional[list[BonFiscalSchema]] = Field(default_factory=list)
    efacturi: Optional[list[EFacturaSchema]] = Field(default_factory=list)
    extrase_bancare: Optional[list[ExtrasBancarLineSchema]] = Field(
        default_factory=list
    )
    facturi: Optional[list[FacturaFiscalaSchema]] = Field(default_factory=list)


class ReportsDataByYear(BaseModel):
    bonuri: Optional[dict[int, list[BonFiscalSchema]]] = Field(default_factory=dict)
    efacturi: Optional[dict[int, list[EFacturaSchema]]] = Field(default_factory=dict)
    extrase_bancare: Optional[dict[int, list[ExtrasBancarLineSchema]]] = Field(
        default_factory=dict
    )
    facturi: Optional[dict[int, list[FacturaFiscalaSchema]]] = Field(
        default_factory=dict
    )
    ani_disponibili: Optional[list[int]] = Field(default_factory=list)


class RegistrulJurnalIncasariSiPlatiSchema(BaseModel):
    nr_crt: Optional[int] = Field(default=None, description="Nr. Crt.")
    data: Optional[date] = Field(
        default=None, description="Data operațiunii de încasare/plată"
    )
    documentul: Optional[str] = Field(
        default=None, description="Documentul (fel, număr)"
    )
    explicatii: Optional[str] = Field(default=None, description="Explicații")
    incasare_numerar: Optional[float] = Field(default=0, description="Încasări Numerar")
    incasare_banca: Optional[float] = Field(default=0, description="Încasări Bancă")
    plati_numerar: Optional[float] = Field(default=0, description="Plăți Numerar")
    plati_banca: Optional[float] = Field(default=0, description="Plăți Bancă")


class RegistrulDeEvidentaFiscalaSchema(BaseModel):
    # anul: int = Field(description="Anul")
    # rectificare: str = Field(description="Rectificare")
    # sursa_de_venit: str = Field(description="Sursa de venit/categorie de venit")
    nr_crt: int = Field(description="Nr. crt.")
    elemente_de_calcul: str = Field(
        description="Elemente de calcul pentru stabilirea venitului net anual/pierderii nete anuale"
    )
    valoare_ron: float = Field(description="Valoare - lei -")


class RegistrulInventarSchema(BaseModel):
    nr_crt: int = Field(description="Nr. Crt.")
    denumirea_elementelor_inventariate: str = Field(
        description="Denumirea elementelor inventariate"
    )
    valoarea_de_inventar: float = Field(description="Valoarea de inventar")


class RaspunsRegistrePFA(BaseModel):
    zip_path: str
    total_documente: Optional[int] = Field(default=0)
    # Bonuri fiscale
    total_documente_bonuri: Optional[int] = Field(default=0)
    # Facturi fiscale
    total_documente_facturi: Optional[int] = Field(default=0)
    # E-facturi
    total_documente_efacturi: Optional[int] = Field(default=0)
    # Extrase bancare
    total_documente_extrase: Optional[int] = Field(default=0)


@dataclass
class AgentRegistrePFA:
    zip_path: str
    # Detalii PFA/SRL
    nume_firma: str
    cui_cif: str
    cod_tva: str
    # LLM
    model: str = "gemini-3.8-flash"
    client: genai.Client | None = None
    api_key: str | None = None

    def __post_init__(self):
        if self.client is None:
            if not self.api_key:
                raise ValueError("AgentRegistrePFA requires a Gemini client or api_key")
            self.client = genai.Client(api_key=self.api_key)
        self.shared_dir = str(make_workdir("pfasimplu_registre_"))
        self.zip_surse_path = Path(self.shared_dir) / "registre" / "surse"
        self.registre_path = Path(self.shared_dir) / "registre"
        self.zip_surse_path.mkdir(parents=True, exist_ok=True)

        self.raport_bonuri_fiscale_path = None
        self.raport_efactura_path = None
        self.raport_extrase_bancare_path = None
        self.raport_facturi_fiscale_path = None

        self.agent_bonuri = AgentBonuriFiscale(
            zip_path=self.zip_path,
            cod_tva=self.cod_tva,
            cui_cif=self.cui_cif,
            model=self.model,
            client=self.client,
        )

        self.agent_efacturi = AgentEFacturi(
            zip_path=self.zip_path,
            cod_tva=self.cod_tva,
            cui_cif=self.cui_cif,
            model=self.model,
            client=self.client,
        )

        self.agent_extrase_bancare = AgentExtraseBancare(
            zip_path=self.zip_path,
            model=self.model,
            client=self.client,
        )

        self.agent_facturi = AgentFacturiFiscale(
            zip_path=self.zip_path,
            cod_tva=self.cod_tva,
            cui_cif=self.cui_cif,
            model=self.model,
            client=self.client,
        )

    def send_message(self, message: str):
        log.info(f"Registre PFA: {message}")

    def get_responses_from_agents(self):
        tasks = [
            delayed(self.agent_bonuri.process_zip)(),
            delayed(self.agent_extrase_bancare.process_zip)(),
        ]

        results = Parallel(n_jobs=len(tasks), prefer="threads")(tasks)

        raspuns_bonuri, raspuns_extrase = results

        # Fill zip extrase bancare pentru consolidare
        if raspuns_extrase:
            self.agent_facturi.zip_path_extrase_bancare = raspuns_extrase.zip_path
            self.agent_efacturi.zip_path_extrase_bancare = raspuns_extrase.zip_path

        tasks = [
            delayed(self.agent_facturi.process_zip)(),
            delayed(self.agent_efacturi.process_zip)(),
        ]

        results = Parallel(n_jobs=len(tasks), prefer="threads")(tasks)

        raspuns_facturi, raspuns_efacturi = results

        return RaspunsProcesareDocumente(
            bonuri=raspuns_bonuri,
            efacturi=raspuns_efacturi,
            extrase_bancare=raspuns_extrase,
            facturi=raspuns_facturi,
        )

    def move_source_zips(
        self, response: RaspunsProcesareDocumente
    ) -> RaspunsProcesareDocumente:
        if response.bonuri:
            src = Path(response.bonuri.zip_path)
            dst = self.zip_surse_path / src.name
            src.rename(dst)
            response.bonuri.zip_path = str(dst)

        if response.efacturi:
            src = Path(response.efacturi.zip_path)
            dst = self.zip_surse_path / src.name
            src.rename(dst)
            response.efacturi.zip_path = str(dst)

        if response.facturi:
            src = Path(response.facturi.zip_path)
            dst = self.zip_surse_path / src.name
            src.rename(dst)
            response.facturi.zip_path = str(dst)

        if response.extrase_bancare:
            src = Path(response.extrase_bancare.zip_path)
            dst = self.zip_surse_path / src.name
            src.rename(dst)
            response.extrase_bancare.zip_path = str(dst)

        # Remove files we don't need anymore
        shared_dirs = [
            self.agent_bonuri.shared_dir,
            self.agent_efacturi.shared_dir,
            self.agent_extrase_bancare.shared_dir,
            self.agent_facturi.shared_dir,
        ]
        for dirpath in shared_dirs:
            safe_rmtree(dirpath)

        return response

    def unzip_and_return_report_path(self, zip_path: str, report_excel_name: str):
        src = Path(zip_path)
        dst_dir = src.parent

        with zipfile.ZipFile(src, "r") as z_file:
            z_file.extractall(dst_dir)

        src.unlink()

        return dst_dir / report_excel_name

    def extract_reports_from_zips(self, response: RaspunsProcesareDocumente):
        if response.bonuri:
            self.raport_bonuri_fiscale_path = self.unzip_and_return_report_path(
                response.bonuri.zip_path, "raport_bonuri_fiscale.xlsx"
            )

        if response.efacturi:
            self.raport_efactura_path = self.unzip_and_return_report_path(
                response.efacturi.zip_path, "raport_efactura.xlsx"
            )

        if response.facturi:
            self.raport_facturi_fiscale_path = self.unzip_and_return_report_path(
                response.facturi.zip_path, "raport_facturi_fiscale.xlsx"
            )

        if response.extrase_bancare:
            self.raport_extrase_bancare_path = self.unzip_and_return_report_path(
                response.extrase_bancare.zip_path, "raport_extrase_bancare.xlsx"
            )

    def get_reports_list(self):
        bonuri_fiscale = None
        if self.raport_bonuri_fiscale_path:
            datali = get_data_from_excel(self.raport_bonuri_fiscale_path)
            if datali:
                bonuri_fiscale = [BonFiscalSchema(**data) for data in datali]

        efacturi = None
        if self.raport_efactura_path:
            datali = get_data_from_excel(self.raport_efactura_path)
            if datali:
                efacturi = [EFacturaSchema(**data) for data in datali]

        extrase_bancare = None
        if self.raport_extrase_bancare_path:
            datali = get_data_from_excel(self.raport_extrase_bancare_path)
            if datali:
                extrase_bancare = [ExtrasBancarLineSchema(**data) for data in datali]

        facturi_fiscale = None
        if self.raport_facturi_fiscale_path:
            datali = get_data_from_excel(self.raport_facturi_fiscale_path)
            if datali:
                facturi_fiscale = [FacturaFiscalaSchema(**data) for data in datali]

        return ReportsData(
            bonuri=bonuri_fiscale,
            efacturi=efacturi,
            extrase_bancare=extrase_bancare,
            facturi=facturi_fiscale,
        )

    def elimina_facturi_duplicate(self, rapoarte: ReportsData):

        if not rapoarte.efacturi:
            return rapoarte

        # Group facturi by duplicate check keys
        facturi_map = defaultdict(list)
        for fact in rapoarte.facturi:
            key = (fact.data_emitere, fact.total_de_plata)
            facturi_map[key].append(fact)

        # Cross-check with e-facturi for duplicates
        facturi_duplicate_nume = set()
        for efact in rapoarte.efacturi:
            key = (efact.data_emitere, efact.total_de_plata)

            posibile_duplicate = facturi_map.get(key, [])

            for fact in posibile_duplicate:
                score = fuzz.token_sort_ratio(
                    efact.produse_sau_servicii.upper(),
                    fact.produse_sau_servicii.upper(),
                )

                if score >= 80:
                    log.debug(f"Factura duplicat. Skip factura: {fact.nume_fisier}")
                    facturi_duplicate_nume.add(fact.nume_fisier)

        # Reset without efactura/factura duplicates
        rapoarte.facturi = [
            f for f in rapoarte.facturi if f.nume_fisier not in facturi_duplicate_nume
        ]

        return rapoarte

    def group_by_year(self, rapoarte: ReportsData):
        ani_disponibili = set()

        bonuri_grupate_pe_an = {}
        if rapoarte.bonuri:
            for bon in rapoarte.bonuri:
                if bon.data.year not in bonuri_grupate_pe_an:
                    bonuri_grupate_pe_an[bon.data.year] = []
                bonuri_grupate_pe_an[bon.data.year].append(bon)
                ani_disponibili.add(bon.data.year)

        facturi_grupate_pe_an = {}
        if rapoarte.facturi:
            for fact in rapoarte.facturi:
                if fact.data_emitere.year not in facturi_grupate_pe_an:
                    facturi_grupate_pe_an[fact.data_emitere.year] = []
                facturi_grupate_pe_an[fact.data_emitere.year].append(fact)
                ani_disponibili.add(fact.data_emitere.year)

        efacturi_grupate_pe_an = {}
        if rapoarte.efacturi:
            for efact in rapoarte.efacturi:
                if efact.data_emitere.year not in efacturi_grupate_pe_an:
                    efacturi_grupate_pe_an[efact.data_emitere.year] = []
                efacturi_grupate_pe_an[efact.data_emitere.year].append(efact)
                ani_disponibili.add(efact.data_emitere.year)

        extrase_grupate_pe_an = {}
        if rapoarte.extrase_bancare:
            for extras in rapoarte.extrase_bancare:
                if extras.data.year not in extrase_grupate_pe_an:
                    extrase_grupate_pe_an[extras.data.year] = []
                extrase_grupate_pe_an[extras.data.year].append(extras)
                ani_disponibili.add(extras.data.year)

        return ReportsDataByYear(
            bonuri=bonuri_grupate_pe_an,
            efacturi=efacturi_grupate_pe_an,
            extrase_bancare=extrase_grupate_pe_an,
            facturi=facturi_grupate_pe_an,
            ani_disponibili=sorted(ani_disponibili),
        )

    def preia_cheltuieli_din_bonuri(
        self, anul: int, rapoarte_grupate_pe_ani: ReportsDataByYear
    ):
        rows: list[RegistrulJurnalIncasariSiPlatiSchema] = []
        for bon in rapoarte_grupate_pe_ani.bonuri[anul]:
            plati_numerar = 0
            plati_banca = 0

            if bon.modalitate_plata == ModalitatePlata.CARD:
                plati_banca = bon.total_de_plata
            elif bon.modalitate_plata == ModalitatePlata.NUMERAR:
                plati_numerar = bon.total_de_plata

            documentul = bon.nume_fisier
            if bon.nume_fisier_extras_bancar:
                documentul = f"{bon.nume_fisier}, {bon.nume_fisier_extras_bancar}"

            tip_cheltuiala = "Cheltuiala"
            if bon.obiect_de_inventar and bon.mijloc_fix:
                tip_cheltuiala = "Cheltuiala mijloc fix"
                documentul = f"{documentul}, {bon.nume_fisier_mijloc_fix}"

            if bon.obiect_de_inventar and not bon.mijloc_fix:
                tip_cheltuiala = "Cheltuiala obiect inventar"

            row = RegistrulJurnalIncasariSiPlatiSchema(
                data=bon.data_tranzactie or bon.data,
                documentul=documentul,
                explicatii=f"{tip_cheltuiala}: {bon.produse_sau_servicii}",
                incasare_numerar=0,
                incasare_banca=0,
                plati_numerar=plati_numerar,
                plati_banca=plati_banca,
            )

            rows.append(row)

        return rows

    def este_o_cheltuiala(self, factura: FacturaFiscalaSchema | EFacturaSchema):
        # Incasare
        scor_incasare_nume_firma = fuzz.token_sort_ratio(
            self.nume_firma.upper(), factura.vanzator_nume.upper()
        )

        scor_incasare_cui = 0
        if self.cui_cif and factura.vanzator_cui:
            scor_incasare_cui = fuzz.ratio(
                self.cui_cif.upper(), factura.vanzator_cui.upper()
            )

        scor_incasare_tva = 0
        if self.cod_tva and factura.vanzator_cui_tva:
            scor_incasare_tva = fuzz.ratio(
                self.cod_tva.upper(), factura.vanzator_cui_tva.upper()
            )

        # Cheltuiala
        scor_cheltuiala_nume_firma = fuzz.token_sort_ratio(
            self.nume_firma.upper(), factura.cumparator_nume.upper()
        )

        scor_cheltuiala_cui = 0
        if self.cui_cif and factura.cumparator_cui:
            scor_cheltuiala_cui = fuzz.ratio(
                self.cui_cif.upper(), factura.cumparator_cui.upper()
            )

        scor_cheltuiala_tva = 0
        if self.cod_tva and factura.cumparator_cui_tva:
            scor_cheltuiala_tva = fuzz.ratio(
                self.cod_tva.upper(), factura.cumparator_cui_tva.upper()
            )

        # Average scores
        avg_incasare = statistics.mean(
            [scor_incasare_nume_firma, scor_incasare_cui, scor_incasare_tva]
        )
        avg_cheltuiala = statistics.mean(
            [
                scor_cheltuiala_nume_firma,
                scor_cheltuiala_cui,
                scor_cheltuiala_tva,
            ]
        )

        return avg_incasare < avg_cheltuiala

    def preia_cheltuieli_si_incasari_din_facturi(
        self, anul: int, rapoarte_grupate_pe_ani: ReportsDataByYear
    ):
        rows: list[RegistrulJurnalIncasariSiPlatiSchema] = []
        for factura in rapoarte_grupate_pe_ani.facturi[anul]:
            documentul = factura.nume_fisier

            if factura.nume_fisier_extras_bancar:
                documentul = (
                    f"{factura.nume_fisier}, {factura.nume_fisier_extras_bancar}"
                )

            plati_banca = 0
            incasare_banca = 0

            if self.este_o_cheltuiala(factura):
                plati_banca = factura.total_de_plata_in_ron

                tip_tranzactie = "Cheltuiala"

                if factura.obiect_de_inventar and factura.mijloc_fix:
                    tip_tranzactie = "Cheltuiala mijloc fix"
                    documentul = f"{documentul}, {factura.nume_fisier_mijloc_fix}"

                if factura.obiect_de_inventar and not factura.mijloc_fix:
                    tip_tranzactie = "Cheltuiala obiect inventar"

            else:
                tip_tranzactie = "Incasare"
                incasare_banca = factura.total_de_plata_in_ron

            row = RegistrulJurnalIncasariSiPlatiSchema(
                data=factura.data_tranzactie or factura.data_emitere,
                documentul=documentul,
                explicatii=f"{tip_tranzactie}: {factura.produse_sau_servicii}",
                incasare_numerar=0,
                incasare_banca=incasare_banca,
                plati_numerar=0,
                plati_banca=plati_banca,
            )
            rows.append(row)

        return rows

    def preia_cheltuieli_si_incasari_din_efacturi(
        self, anul: int, rapoarte_grupate_pe_ani: ReportsDataByYear
    ):
        rows: list[RegistrulJurnalIncasariSiPlatiSchema] = []
        for efactura in rapoarte_grupate_pe_ani.efacturi[anul]:
            documentul = efactura.nume_fisier
            if efactura.nume_fisier_extras_bancar:
                documentul = (
                    f"{efactura.nume_fisier}, {efactura.nume_fisier_extras_bancar}"
                )

            plati_banca = 0
            incasare_banca = 0
            incasare_numerar = 0
            plati_numerar = 0

            expense = self.este_o_cheltuiala(efactura)

            if expense:
                tip_tranzactie = "Cheltuiala"

                if efactura.obiect_de_inventar and efactura.mijloc_fix:
                    tip_tranzactie = "Cheltuiala mijloc fix"
                    documentul = f"{documentul}, {efactura.nume_fisier_mijloc_fix}"

                if efactura.obiect_de_inventar and not efactura.mijloc_fix:
                    tip_tranzactie = "Cheltuiala obiect inventar"

            else:
                tip_tranzactie = "Incasare"

            if efactura.modalitate_plata == ModalitatePlata.CARD and expense:
                plati_banca = efactura.total_de_plata
            elif efactura.modalitate_plata == ModalitatePlata.CARD and not expense:
                incasare_banca = efactura.total_de_plata
            # NOTE: Toate sunt prin CARD
            # elif efactura.modalitate_plata == ModalitatePlata.NUMERAR and expense:
            #     plati_numerar = efactura.total_de_plata
            # elif efactura.modalitate_plata == ModalitatePlata.NUMERAR and not expense:
            #     incasare_numerar = efactura.total_de_plata

            row = RegistrulJurnalIncasariSiPlatiSchema(
                data=efactura.data_tranzactie or efactura.data_emitere,
                documentul=documentul,
                explicatii=f"{tip_tranzactie}: {efactura.produse_sau_servicii}",
                incasare_numerar=incasare_numerar,
                incasare_banca=incasare_banca,
                plati_numerar=plati_numerar,
                plati_banca=plati_banca,
            )

            rows.append(row)

        return rows

    def preia_comisioane_diferente_schimb_valutar_din_extrase_bancare(
        self, anul: int, rapoarte_grupate_pe_ani: ReportsDataByYear
    ):
        rows: list[RegistrulJurnalIncasariSiPlatiSchema] = []
        for extras in rapoarte_grupate_pe_ani.extrase_bancare[anul]:
            plati_banca = 0
            tip_cheltuiala = None

            if extras.comision_bancar:
                tip_cheltuiala = "Comision bancar"
                plati_banca = extras.comision_bancar

            if extras.diferenta_curs_valutar:
                tip_cheltuiala = "Diferente curs valutar"
                plati_banca = extras.diferenta_curs_valutar

            if not tip_cheltuiala:
                continue

            row = RegistrulJurnalIncasariSiPlatiSchema(
                data=extras.data,
                documentul=extras.nume_fisier,
                explicatii=f"Cheltuiala: {tip_cheltuiala} {extras.denumire_cont}",
                incasare_numerar=0,
                incasare_banca=0,
                plati_numerar=0,
                plati_banca=plati_banca,
            )

            rows.append(row)
        return rows

    def creeaza_rjip_pe_ani(
        self, rapoarte_grupate_pe_ani: ReportsDataByYear
    ) -> dict[int, list[RegistrulJurnalIncasariSiPlatiSchema]]:
        rjip_pe_ani = {}

        for anul in rapoarte_grupate_pe_ani.ani_disponibili:
            if anul not in rjip_pe_ani:
                rjip_pe_ani[anul] = []

            # Bonuri fiscale
            if anul in rapoarte_grupate_pe_ani.bonuri:
                cheltuieli_din_bonuri = self.preia_cheltuieli_din_bonuri(
                    anul, rapoarte_grupate_pe_ani
                )
                rjip_pe_ani[anul].extend(cheltuieli_din_bonuri)

            # Facturi fiscale
            if anul in rapoarte_grupate_pe_ani.facturi:
                cheltuieli_si_incasari_din_facturi = (
                    self.preia_cheltuieli_si_incasari_din_facturi(
                        anul, rapoarte_grupate_pe_ani
                    )
                )
                rjip_pe_ani[anul].extend(cheltuieli_si_incasari_din_facturi)

            # E-facturi
            if anul in rapoarte_grupate_pe_ani.efacturi:
                cheltuieli_si_incasari_din_efacturi = (
                    self.preia_cheltuieli_si_incasari_din_efacturi(
                        anul, rapoarte_grupate_pe_ani
                    )
                )
                rjip_pe_ani[anul].extend(cheltuieli_si_incasari_din_efacturi)

            # Extrase bancare
            if anul in rapoarte_grupate_pe_ani.extrase_bancare:
                comisioane_diferente_schimb_valutar = (
                    self.preia_comisioane_diferente_schimb_valutar_din_extrase_bancare(
                        anul, rapoarte_grupate_pe_ani
                    )
                )
                rjip_pe_ani[anul].extend(comisioane_diferente_schimb_valutar)

        return rjip_pe_ani

    def genereaza_rjip(
        self, rapoarte_grupate_pe_ani: ReportsDataByYear
    ) -> dict[int, list[RegistrulJurnalIncasariSiPlatiSchema]]:
        rjip_pe_ani = self.creeaza_rjip_pe_ani(rapoarte_grupate_pe_ani)

        rjip_gata_de_inserat = {}
        for anul, rows in rjip_pe_ani.items():
            # 1. Sort and assign nr_crt
            rows = sorted(rows, key=lambda r: r.data)
            for idx, row in enumerate(rows):
                row.nr_crt = idx + 1

            # 2. Group by month to calculate totals
            rows_by_month: dict[int, list[RegistrulJurnalIncasariSiPlatiSchema]] = {}
            for row in rows:
                if row.data.month not in rows_by_month:
                    rows_by_month[row.data.month] = []
                rows_by_month[row.data.month].append(row)

            # 3. Create the summary objects
            month_summaries = {}
            for month, month_rows in rows_by_month.items():
                month_summaries[month] = RegistrulJurnalIncasariSiPlatiSchema(
                    explicatii="TOTAL LUNA:",
                    incasare_numerar=sum(r.incasare_numerar for r in month_rows),
                    incasare_banca=sum(r.incasare_banca for r in month_rows),
                    plati_numerar=sum(r.plati_numerar for r in month_rows),
                    plati_banca=sum(r.plati_banca for r in month_rows),
                )

            # 4. Build the final list with injected totals
            final_rows_with_totals = []
            for i, row in enumerate(rows):
                final_rows_with_totals.append(row)

                # Check if the next row is a different month OR if this is the last row in the year
                is_last_row = i == len(rows) - 1
                is_month_changing = not is_last_row and (
                    rows[i + 1].data.month != row.data.month
                )

                if is_month_changing or is_last_row:
                    final_rows_with_totals.append(month_summaries[row.data.month])

            # 5. Store the result for the year
            rjip_gata_de_inserat[anul] = final_rows_with_totals

        # Save to excel
        rjip_paths = []
        for anul, rows in rjip_gata_de_inserat.items():
            if not rows:
                continue

            rows_dict = [r.model_dump() for r in rows]

            rjip_path = create_excel(
                rows_dict,
                output_path=f"{self.registre_path}/registrul_jurnal_de_incasari_si_plati_pentru_anul_{anul}.xlsx",
            )
            rjip_paths.append(rjip_path)

        return rjip_gata_de_inserat

    def genereaza_ref(
        self, rjip_pe_ani: dict[int, list[RegistrulJurnalIncasariSiPlatiSchema]]
    ) -> dict[int, list[RegistrulDeEvidentaFiscalaSchema]]:
        ref_pe_ani = {}
        for anul, rows in rjip_pe_ani.items():
            if anul not in ref_pe_ani:
                ref_pe_ani[anul] = []

            # TOTAL Incasari/Cheltuieli pentru anul respectiv cu calcul amortizari mijloace fixe
            total_incasari = 0
            total_cheltuieli = 0
            for row in rows:
                if "TOTAL LUNA:" in row.explicatii:
                    continue

                incasare = row.incasare_banca + row.incasare_numerar
                total_incasari += incasare

                plata = row.plati_banca + row.plati_numerar
                if not row.explicatii.startswith("Cheltuiala mijloc fix:"):
                    total_cheltuieli += plata
                else:
                    # Extragem numele fisei mijloc fix
                    match = re.search(r"fmj_\w+\.xlsx", row.documentul)
                    filename = match.group(0)

                    fmj_paths: list[Path] = [
                        self.zip_surse_path / "facturi_fiscale" / filename,
                        self.zip_surse_path / "efacturi" / filename,
                        self.zip_surse_path / "bonuri_fiscale" / filename,
                    ]

                    amortizare_valoare = 0
                    for fmj_path in fmj_paths:
                        # Fisierul a fost gasit
                        if amortizare_valoare > 0:
                            break

                        if not fmj_path.exists():
                            continue

                        datali = get_data_from_excel(fmj_path)
                        for data in datali:
                            if not data["Coloana"].startswith(
                                "amortizare_pentru_anul_"
                            ):
                                continue
                            amortizare_an = int(data["Coloana"].split("_")[-1])
                            if anul == amortizare_an:
                                amortizare_valoare = float(data["Valoare"])
                                break

                    total_cheltuieli += amortizare_valoare

            ref_pe_ani[anul] = [
                RegistrulDeEvidentaFiscalaSchema(
                    nr_crt=1,
                    elemente_de_calcul="Total venituri din activitati independente",
                    valoare_ron=round(total_incasari, 2),
                ),
                RegistrulDeEvidentaFiscalaSchema(
                    nr_crt=2,
                    elemente_de_calcul="Total cheltuieli deductibile",
                    valoare_ron=round(total_cheltuieli, 2),
                ),
            ]

        ref_paths = []
        for anul, rows in ref_pe_ani.items():
            if not rows:
                continue

            rows_dict = [r.model_dump() for r in rows]

            ref_path = create_excel(
                rows_dict,
                output_path=f"{self.registre_path}/registrul_de_evidenta_fiscala_pentru_anul_{anul}.xlsx",
            )
            ref_paths.append(ref_path)

        return ref_pe_ani

    def genereaza_ri(
        self, rjip_pe_ani: dict[int, list[RegistrulJurnalIncasariSiPlatiSchema]]
    ) -> dict[int, list[RegistrulInventarSchema]]:
        ri_pe_ani = {}
        for anul, rows in rjip_pe_ani.items():
            nr_crt = 0
            if anul not in ri_pe_ani:
                ri_pe_ani[anul] = []

            for row in rows:
                if not row.explicatii.startswith(
                    ("Cheltuiala mijloc fix:", "Cheltuiala obiect inventar:")
                ):
                    continue

                valoare = round(row.plati_banca + row.plati_numerar, 2)

                # Un obiect returnat suma a venit in cont dupa returnare
                if valoare < 0:
                    continue

                nume_obiect = row.explicatii.replace(
                    "Cheltuiala obiect inventar:", ""
                ).replace("Cheltuiala mijloc fix:", "")

                nr_crt += 1
                item = RegistrulInventarSchema(
                    nr_crt=nr_crt,
                    denumirea_elementelor_inventariate=nume_obiect,
                    valoarea_de_inventar=valoare,
                )

                ri_pe_ani[anul].append(item)

        ri_paths = []
        for anul, rows in ri_pe_ani.items():
            if not rows:
                continue

            rows_dict = [r.model_dump() for r in rows]

            ri_path = create_excel(
                rows_dict,
                output_path=f"{self.registre_path}/registrul_inventar_pentru_anul_{anul}.xlsx",
            )
            ri_paths.append(ri_path)

        return ri_pe_ani

    def muta_fisa_mijloc_fix_in_registre(self):
        search_pattern = os.path.join(self.zip_surse_path, "**", "*.xlsx")
        fmj_regex = r"fmj_\w+\.xlsx"

        for file_path in glob.glob(search_pattern, recursive=True):
            filename = os.path.basename(file_path)

            if re.search(fmj_regex, filename):
                dest_path = os.path.join(self.registre_path, filename)

                try:
                    shutil.move(file_path, dest_path)
                except Exception as e:
                    log.error(f"Nu am putut muta {file_path} eroare: {e}")

    def make_final_response(self, zip_path: Path, response: RaspunsProcesareDocumente):
        final_response = RaspunsRegistrePFA(zip_path=str(zip_path))

        if response.bonuri:
            final_response.total_documente += response.bonuri.total_documente
            final_response.total_documente_bonuri += response.bonuri.total_documente

        if response.facturi:
            final_response.total_documente += response.facturi.total_documente
            final_response.total_documente_facturi = response.facturi.total_documente

        if response.efacturi:
            final_response.total_documente += response.efacturi.total_documente
            final_response.total_documente_efacturi = response.efacturi.total_documente

        if response.extrase_bancare:
            final_response.total_documente += response.extrase_bancare.total_documente
            final_response.total_documente_extrase = (
                response.extrase_bancare.total_documente
            )

        log.debug(
            f"total_documente: {final_response.total_documente}"
        )

        return final_response

    def process_zip(self):
        try:
            response = self.get_responses_from_agents()
            response = self.move_source_zips(response)
            self.extract_reports_from_zips(response)

            rapoarte = self.get_reports_list()
            rapoarte = self.elimina_facturi_duplicate(rapoarte)
            rapoarte_grupate_pe_ani = self.group_by_year(rapoarte)

            rjip_pe_ani = self.genereaza_rjip(rapoarte_grupate_pe_ani)
            self.genereaza_ref(rjip_pe_ani)
            self.genereaza_ri(rjip_pe_ani)
            self.muta_fisa_mijloc_fix_in_registre()

            # Make ZIP report
            report_dir = Path(self.registre_path)
            shared_dir = Path(self.shared_dir)

            zip_name = f"registre_pfa_{uuid.uuid4().hex}.zip"
            zip_path = shared_dir / zip_name

            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
                for file_path in report_dir.rglob("*"):
                    if file_path.is_file():
                        # keep paths relative to report_dir
                        z.write(file_path, arcname=file_path.relative_to(report_dir))

            final_response = self.make_final_response(zip_path, response)
            return final_response

        except Exception as ex:
            log.exception(ex)
            return

    def cleanup(self):
        safe_rmtree(self.shared_dir)
