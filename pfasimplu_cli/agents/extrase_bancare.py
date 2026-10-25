import hashlib
import os
import pathlib
import uuid
import zipfile
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Generator, Optional

from google import genai
from google.genai import types
from joblib import Parallel, delayed
from loguru import logger as log
from pydantic import BaseModel, Field
from pypdf import PdfReader
from tenacity import retry, stop_after_attempt, stop_after_delay, wait_random
from unidecode import unidecode

from pfasimplu_cli.exchange_rates import Currency, ron_exchange_rate
from pfasimplu_cli.utils import (
    create_excel,
    extract_json_from_llm_response,
    jitter_call,
    make_workdir,
    safe_rmtree,
)

from .common import RaspunsRaport


class PDFFile(BaseModel):
    pdf_name: str
    pdf_path: str
    pdf_pages: int


class DetinatorExtrasBancarSchema(BaseModel):
    # Date Cont
    nume_detinator_cont: str = Field(
        max_length=250,
        description="Numele complet al deținătorului contului bancar din care provine extrasul.",
    )
    denumire_cont: str = Field(
        max_length=250,
        description="Denumirea sau aliasul contului (Ex: Cont Curent RON, Cont Economii EUR, Cont de Card).",
    )
    valuta_cont: Currency = Field(
        max_length=3,
        description="Codul valutar ISO 4217 al contului bancar (Ex: RON, EUR, USD).",
    )
    iban_cont: str = Field(
        max_length=250, description="Contul IBAN complet al contului bancar."
    )


class TranzactieSchema(BaseModel):
    # Detalii Tranzacție
    data_transfer: date = Field(
        description="Data exactă la care a fost executat transferul sau operațiunea bancară, în format YYYY-MM-DD.",
    )
    descriere_transfer: str = Field(
        max_length=5000,
        description="Descrierea completă a tranzacției, care poate include detalii despre plată, număr factură, destinatar, IBAN, sau explicații bancare.",
    )
    nume_parte_din_descriere_transfer: str = Field(
        max_length=5000,
        description="Numele entitatii de la care sau catre care s-a efectuat transferul/operatiunea bancara preluata din campul 'descriere_transfer'",
    )
    iban_din_descriere_transfer: Optional[str] = Field(
        default=None,
        max_length=250,
        description="Contul IBAN complet din campul 'descriere_transfer' daca este disponibil.",
    )
    transfer_intern_intre_conturi: Optional[bool] = Field(
        default=None,
        description="True daca transferul a fost facut intre conturile IBAN ale detinatorului contului, False daca esti sigur ca nu a fost facut intre conturile detinute. None daca nu esti sigur.",
    )

    # Sume
    suma_debit: Optional[float] = Field(
        default=0,
        description="Suma de bani ieșită din cont (debitare). Doar valori pozitive. (Cu 2 zecimale). Ex: -30 > 30",
    )
    suma_credit: Optional[float] = Field(
        default=0,
        description="Suma de bani intrată în cont (creditare). Doar valori pozitive. (Cu 2 zecimale).",
    )

    suma_debit_in_ron: Optional[float] = Field(
        default=0,
        description="E la fel ca suma_debit cu mentiunea ca daca contul este un alta valuta ex: USD - suma va fi automat convertita in ron. Nu completa acest camp.",
    )
    suma_credit_in_ron: Optional[float] = Field(
        default=0,
        description="E la fel ca suma_credit cu mentiunea ca daca contul este un alta valuta ex: USD - suma va fi automat convertita in ron. Nu completa acest camp.",
    )

    # Detalii Schimb Valutar
    suma_valuta_schimbata: Optional[float] = Field(
        default=0,
        description="Suma schimbata dintr-o valuta precum EUR, USD etc. in RON (Adauga toate zecimale disponibile).",
    )
    curs_valutar_aplicat: Optional[float] = Field(
        default=0,
        description="Cursul de schimb real aplicat de bancă pentru această tranzacție valutară (Adauga toate zecimale disponibile).",
    )
    curs_valutar_bnr: Optional[float] = Field(
        default=0,
        description="Cursul de referință BNR valabil la data transferului. Nu completa acest camp",
    )
    diferenta_curs_valutar_deductibila_in_ron: Optional[float] = Field(
        default=0,
        description="Suma in RON deductibila rezultata in urma calculului 'suma_valuta_schimbata' 'curs_valutar_aplicat' si 'curs_valutar_bnr'. Nu completa acest camp",
    )
    comision_bancar_deductibil_in_ron: Optional[float] = Field(
        default=0,
        description="Comisionul bancar deductibil in RON. Daca contul bancar este in USD suma va fi convertita in RON. Nu completa acest camp.",
    )


class ExtrasBancarSchema(BaseModel):
    proprietar_cont_bancar: DetinatorExtrasBancarSchema
    tranzactii: list[TranzactieSchema]


class RaspunsProcesare(BaseModel):
    extras_bancar: Optional[ExtrasBancarSchema] = None
    nume_fisier_pdf: Optional[str] = None
    numar_pagini_pdf: Optional[int] = 0
    eroare: bool = False


class ExtrasBancarLineSchema(BaseModel):
    nume_fisier: str
    # Date cont
    nume_cont: str
    denumire_cont: str
    valuta_cont: str
    iban: str
    # Detalii Tranzacție
    data: date
    descriere: str
    nume_extras: Optional[str]
    iban_extras: Optional[str]
    transfer_intern: Optional[bool]
    # Sume
    debit: Optional[float]
    credit: Optional[float]
    debit_ron: Optional[float]
    credit_ron: Optional[float]
    # Detalii Schimb Valutar (dacă există)
    valuta_schimbata: Optional[float]
    curs_banca: Optional[float]
    curs_bnr: Optional[float]
    diferenta_curs_valutar: Optional[float]
    comision_bancar: Optional[float]


@dataclass
class AgentExtraseBancare:
    zip_path: str
    model: str = "gemini-3.8-flash"
    client: genai.Client | None = None
    api_key: str | None = None

    def __post_init__(self):
        self.pdfs = None
        if self.client is None:
            if not self.api_key:
                raise ValueError("AgentExtraseBancare requires a Gemini client or api_key")
            self.client = genai.Client(api_key=self.api_key)
        self.shared_dir = str(make_workdir("pfasimplu_extrase_"))
        self.extrase_bancare_path = (
            Path(self.shared_dir) / "extrase_bancare_colectate" / "extrase_bancare"
        )
        self.report_path = Path(self.shared_dir) / "extrase_bancare_colectate"
        self.extrase_bancare_path.mkdir(parents=True, exist_ok=True)

    def send_message(self, message: str):
        log.info(f"Extrase bancare: {message}")

    @property
    def system_prompt(self):
        prompt = """
        Ești un expert în analizarea extraselor bancare (Bank Statements) din România. 
        Misiunea ta este să extragi cu PRECIZIE MAXIMĂ toate datele din PDF-ul primit și să le transformi în format JSON conform schemei 'ExtrasBancarSchema'.

        ═══════════════════════════════════════════════════════════════════════════════
        1. STRUCTURA GENERALĂ A EXTRASULUI BANCAR
        ═══════════════════════════════════════════════════════════════════════════════

        PDF-ul poate conține una sau mai multe pagini și are următoarea structură:

        A. ANTET (prima secțiune):
        - Numele deținătorului contului
        - IBAN-ul contului
        - Denumirea contului (Ex: "Principal Current Account", "Current Account", "Cont Curent")
        - Valuta contului (RON, USD, EUR, etc.)
        - Date despre bancă

        B. TABEL CU TRANZACȚII (secțiunea principală):
        - Conține toate operațiunile bancare din perioada extrasului
        - ATENȚIE CRITICĂ: O linie de tabel (row) ÎNCEPE ÎNTOTDEAUNA cu DATA operațiunii
        - Capul de tabel (header) se poate întinde pe 2-3 rânduri consecutive

        ═══════════════════════════════════════════════════════════════════════════════
        2. IDENTIFICAREA COLOANELOR DIN TABEL
        ═══════════════════════════════════════════════════════════════════════════════

        Coloanele pot avea denumiri diferite în funcție de bancă, dar ORDINEA rămâne similară:

        ┌─────────────────────────────────────────────────────────────────────────────┐
        │ COLOANĂ               │ VARIAȚII POSIBILE DE DENUMIRE                       │
        ├─────────────────────────────────────────────────────────────────────────────┤
        │ Data Transfer         │ "Book Date", "Data tranzactiei", "Data operatiune", │
        │                       │ "Trans.Date", "Data oper.", "Value date"            │
        ├─────────────────────────────────────────────────────────────────────────────┤
        │ Descriere Transfer    │ "Transaction Description", "Counterparty",          │
        │                       │ "Descriere operatiune",                             │
        ├─────────────────────────────────────────────────────────────────────────────┤
        │ Debit (ieșiri)        │ "Debit", "Debits", "-" (coloană cu valori negative) │
        ├─────────────────────────────────────────────────────────────────────────────┤
        │ Credit (intrări)      │ "Credit", "Credits", "+" (coloană valori pozitive)  │
        └─────────────────────────────────────────────────────────────────────────────┘

        ═══════════════════════════════════════════════════════════════════════════════
        3. EXTRAGEREA DATEI TRANSFERULUI
        ═══════════════════════════════════════════════════════════════════════════════

        Formatul de intrare (variabil):
        - DD-MM-YYYY (Ex: 07-12-2025)
        - DD/MM/YYYY (Ex: 23/01/2024)
        - DD.MM.YYYY (Ex: 15.07.2025)

        ⚠️ CONVERSIE OBLIGATORIE: Transformă ÎNTOTDEAUNA în format ISO: YYYY-MM-DD
        Exemplu: 07-12-2025 → 2025-12-07

        ═══════════════════════════════════════════════════════════════════════════════
        4. EXTRAGEREA DESCRIERII TRANSFERULUI
        ═══════════════════════════════════════════════════════════════════════════════

        🔴 REGULĂ CRITICĂ: Copiază INTEGRAL textul din coloana descrierii, inclusiv:
        - Toate liniile de text (separate prin \n)
        - Detalii despre card (număr parțial: xxxx xxxx xxxx 8762)
        - IBAN-uri
        - Coduri de autorizare
        - Referințe bancare

        Această coloană se află aproape ÎNTOTDEAUNA imediat după coloana "Data transfer".

        ═══════════════════════════════════════════════════════════════════════════════
        5. IDENTIFICAREA 'nume_parte_din_descriere_transfer'
        ═══════════════════════════════════════════════════════════════════════════════

        Analiza este DIFERITĂ în funcție de tipul de operațiune:

        ┌─────────────────────────────────────────────────────────────────────────────┐
        │ TIP OPERAȚIUNE                    │ CUM IDENTIFICI NUMELE PĂRȚII            │
        ├─────────────────────────────────────────────────────────────────────────────┤
        │ A) PLATĂ LA COMERCIANT (POS)      │                                         │
        ├─────────────────────────────────────────────────────────────────────────────┤
        │ Exemplu descriere:                                                          │
        │ "PayU*inregistraredomenii         │ → NUME: "PayU*inregistraredomenii"      │
        │  POS purchase CLIMENTE ALIN       │                                         │
        │  Card No: xxxx xxxx xxxx 8762     │ Logică: Comerciantul este pe primul     │
        │  Date: 02-09-2025"                │ rând, clientul face achiziția DE LA el  │
        ├─────────────────────────────────────────────────────────────────────────────┤
        │ Exemplu 2:                                                                  │
        │ "NETFLIX.COM 866-579-7172 NLD     │ → NUME: "NETFLIX.COM"                   │
        │  Orig: 5.99 EUR Curs: 5.1652"     │                                         │
        ├─────────────────────────────────────────────────────────────────────────────┤
        │ B) TRANSFER BANCAR                │                                         │
        ├─────────────────────────────────────────────────────────────────────────────┤
        │ Exemplu descriere:                                                          │
        │ "Alin Climente ING Cont Current   │ → NUME: "Alin Climente ING Cont Current"│
        │  RO53INGB0000999912712486         │                                         │
        │  Transfer ING Business..."        │ Logică: Beneficiarul transferului este  │
        │                                   │ pe primul rând sau după IBAN            │
        ├─────────────────────────────────────────────────────────────────────────────┤
        │ Exemplu 2:                                                                  │
        │ "GENPACT ROMANIA SRL              │ → NUME: "GENPACT ROMANIA SRL"           │
        │  RO25CITI0000000799512057         │                                         │
        │  Transfer credit- Incasare"       │                                         │
        ├─────────────────────────────────────────────────────────────────────────────┤
        │ Exemplu 3 (intrare fonduri):                                                │
        │ "1NCUBE LIMITED                   │ → NUME: "1NCUBE LIMITED"                │
        │  GB70REVO00996974413902           │                                         │
        │  Incoming funds INV 2506"         │ Logică: Expeditorul este pe primul rând │
        ├─────────────────────────────────────────────────────────────────────────────┤
        │ C) COMISION BANCAR                │                                         │
        ├─────────────────────────────────────────────────────────────────────────────┤
        │ Exemplu 1: "Service Fee"          │ → NUME: "Comision Bancar"               │
        │ Exemplu 2: "Service Fee Account   │ → NUME: "Comision Bancar"               │
        │            administration fee: 30"│                                         │
        │                                   │                                         │
        │ Logică: Dacă textul conține "Fee" │ Tradus: Service Fee = Comision Bancar   │
        │ și NU există alte detalii (IBAN,  │                                         │
        │ nume părți), este comision bancar │                                         │
        ├─────────────────────────────────────────────────────────────────────────────┤
        │ D) RETRAGERE NUMERAR ATM          │                                         │
        ├─────────────────────────────────────────────────────────────────────────────┤
        │ Exemplu:                          │ → NUME: "Retragere Numerar ATM"         │
        │ "Retrageri de numerar-ATM alte BC"│                                         │
        │ sau "ATM Withdrawal"              │                                         │
        └─────────────────────────────────────────────────────────────────────────────┘

        ═══════════════════════════════════════════════════════════════════════════════
        6. EXTRAGEREA IBAN-ULUI DIN DESCRIERE
        ═══════════════════════════════════════════════════════════════════════════════

        Format IBAN (ISO 13616):
        ✓ Începe cu 2 litere (cod țară): RO, GB, FR, ES, DE, NO, etc.
        ✓ Urmat de 2 cifre de control
        ✓ Apoi între 10-30 caractere alfanumerice
        ✓ Poate avea sau nu spații între grupuri

        Exemple valide:
        • "RO04INGB0000999911194833"
        • "RO04 INGB 0000 9999 1119 4833"
        • "GB70REVO00996974413902"
        • "GB70 REVO 0099 6974 4139 02"
        • "FR76 3000 6000 0112 3456 7890 189"

        🔍 CĂUTARE: Scanează întreaga descriere și extrage primul IBAN găsit.
        ⚠️ CURĂȚARE: Elimină spațiile pentru uniformizare în JSON.

        ═══════════════════════════════════════════════════════════════════════════════
        7. EXTRAGEREA SUMELOR (DEBIT ȘI CREDIT)
        ═══════════════════════════════════════════════════════════════════════════════

        ┌─────────────────────────────────────────────────────────────────────────────┐
        │ COLOANĂ  │ CE REPREZINTĂ        │ CONVERSIE                                 │
        ├─────────────────────────────────────────────────────────────────────────────┤
        │ Debit    │ Bani IEȘIȚI din cont │ Valoare ABSOLUTĂ (pozitivă)               │
        │          │ Exemple: -45, -67.78 │ → stochează: 45.00, 67.78                 │
        ├─────────────────────────────────────────────────────────────────────────────┤
        │ Credit   │ Bani INTRAȚI în cont │ Valoare pozitivă (fără modificări)        │
        │          │ Exemple: 4567.67     │ → stochează: 4567.67                      │
        └─────────────────────────────────────────────────────────────────────────────┘

        🔴 ATENȚIE: Folosește ÎNTOTDEAUNA 2 zecimale (format float cu .2f)

        ═══════════════════════════════════════════════════════════════════════════════
        8. DETECTAREA SCHIMBULUI VALUTAR
        ═══════════════════════════════════════════════════════════════════════════════

        Dacă în descriere găsești informații despre schimb valutar, extrage următoarele:

        📋 EXEMPLUL 1 (Plata din RON intr-o valuta straina ca EUR/USD etc.):
        "Orig: 5.99 EUR Curs: 5.1652 RON/EUR"
        
        ├─ suma_valuta_schimbata = 5.99
        ├─ valuta_tranzactie = "EUR"
        └─ curs_valutar_aplicat = 5.1652

        Observatii:
        - Cazul de sus este pentru banca BRD. Alte banci pot avea o forma diferita 
        - Identifica suma schimbata, moneda/valuta si cursul valutar aplicat (toate aceste informatii le vei gasi in descriere).

        📋 EXEMPLUL 2 (Schimbul valutar dintr-o valuta straina USD/EUR etc in RON):
        "Foreign exchange ING Business Amount: 42.182,00 RON Rate: 4,21820"
        
        ├─ suma_valuta_schimbata = 42182.00  - suma care a fost schimbata din moneda straina USD/EUR etc in RON
        └─ curs_valutar_aplicat = 4.21820    - acesta este cursul valutar aplicat de banca

        Observatii:
        - Cazul de sus este pentru banca ING. Alte banci pot avea o forma diferita.
        - Identifica suma schimbata (o gasesti in descriere in RON) si cursul valutar aplicat de banca (il vei gasi tot in descriere).

                
        ⚠️ NOTĂ IMPORTANTĂ: Păstrează TOATE zecimalele disponibile pentru cursul valutar!

        ═══════════════════════════════════════════════════════════════════════════════
        9. DETECTAREA TRANSFERURILOR INTERNE
        ═══════════════════════════════════════════════════════════════════════════════

        🔍 LOGICĂ DE VERIFICARE:
        
        IF 'nume_detinator_cont' apare în 'descriere_transfer':
            transfer_intern_intre_conturi = True
        ELSE IF descrierea conține "transfer intern" SAU "transfer personal" SAU "transfer intre conturi" SAU "aport capital" SAU ambele IBAN-uri aparțin aceluiași titular:
            transfer_intern_intre_conturi = True
        ELSE:
            transfer_intern_intre_conturi = False

        Exemple:
        • "Alin Climente ING Cont Current..." → True (numele deținătorului apare)
        • "Transfer ING Business transfer intern" → True (conține "transfer intern")
        • Transfer între RO04INGB0000999911194833 și RO21INGB0000999916538451 ambele pe numele "CLIMENTE ALIN" → True

        ═══════════════════════════════════════════════════════════════════════════════
        10. CÂMPURI CARE NU TREBUIE COMPLETATE
        ═══════════════════════════════════════════════════════════════════════════════

        ❌ NU completa următoarele câmpuri (vor fi calculate/completate ulterior):
        • curs_valutar_bnr
        • suma_debit_in_ron
        • suma_credit_in_ron
        • diferenta_curs_valutar_deductibila_in_ron
        • comision_bancar_deductibil_in_ron

        Lasă aceste câmpuri ca None în JSON.

        ═══════════════════════════════════════════════════════════════════════════════
        11. CERINȚE FINALE DE CALITATE
        ═══════════════════════════════════════════════════════════════════════════════

        ✅ VERIFICARE FINALĂ înainte de returnarea JSON:

        1. Ai extras TOATE tranzacțiile din tabel (nu doar primele/ultimele)?
        2. Fiecare dată este în format ISO (YYYY-MM-DD)?
        3. Descrierile sunt COMPLETE (inclusiv toate liniile cu \n)?
        4. Sumele sunt în valoare ABSOLUTĂ pozitivă?
        5. IBAN-urile sunt fără spații și cu literele țării la început?
        6. Ai identificat corect beneficiarii/plătitorii pentru fiecare tranzacție?
        7. Datele despre schimb valutar sunt complete (acolo unde există)?

        ═══════════════════════════════════════════════════════════════════════════════

        🎯 OBIECTIV FINAL: Returnează un JSON valid conform 'ExtrasBancarSchema' cu:
        • proprietar_cont_bancar: obiect DetinatorExtrasBancarSchema complet
        • tranzactii: listă completă de obiecte TranzactieSchema (TOATE din PDF)

        Precizie = 100%. Fiecare detaliu contează pentru contabilitate și fiscalitate.
        """
        return prompt

    @retry(
        stop=(stop_after_delay(600) | stop_after_attempt(2)),
        wait=wait_random(min=1, max=5),
    )
    @jitter_call(3, 6)
    def get_llm_response(
        self, pdf_path: str, temperature: float = 0
    ) -> ExtrasBancarSchema:
        response = self.client.models.generate_content(
            model=self.model,
            contents=[
                types.Part.from_bytes(
                    data=pathlib.Path(pdf_path).read_bytes(),
                    mime_type="application/pdf",
                ),
                "Extrage corect datele din acest extras bancar",
            ],
            config=types.GenerateContentConfig(
                system_instruction=self.system_prompt,
                temperature=temperature,
                response_mime_type="application/json",
                response_json_schema=ExtrasBancarSchema.model_json_schema(),
            ),
        )

        response_schema = ExtrasBancarSchema.model_validate_json(
            extract_json_from_llm_response(response.text)
        )

        return response_schema

    def proceseaza_pdf_extras_bancar(self, pdf_path: str):
        pdf_name = os.path.basename(pdf_path)
        self.send_message(f"Procesam extrasul bancar '{pdf_name}'...")

        try:
            extras_bancar = self.get_llm_response(pdf_path)
            self.send_message(
                f"Am extras {len(extras_bancar.tranzactii)} tranzactii din '{pdf_name}'!"
            )

            valuta_cont = extras_bancar.proprietar_cont_bancar.valuta_cont

            # Cheltuieli in relatie cu banca (administrare cont, comisioane, diferente schimb valutar)
            for tranzactie in extras_bancar.tranzactii:
                # Converteste sumele din valuta straina in ron daca este cazul
                if valuta_cont == "RON":
                    tranzactie.suma_debit_in_ron = tranzactie.suma_debit
                    tranzactie.suma_credit_in_ron = tranzactie.suma_credit

                if valuta_cont != "RON":
                    if tranzactie.suma_debit:
                        tranzactie.suma_debit_in_ron = ron_exchange_rate(
                            amount=tranzactie.suma_debit,
                            currency=valuta_cont,
                            date=tranzactie.data_transfer,
                        )
                        tranzactie.curs_valutar_bnr = ron_exchange_rate(
                            amount=1,
                            currency=valuta_cont,
                            date=tranzactie.data_transfer,
                        )

                    if tranzactie.suma_credit:
                        tranzactie.suma_credit_in_ron = ron_exchange_rate(
                            amount=tranzactie.suma_credit,
                            currency=valuta_cont,
                            date=tranzactie.data_transfer,
                        )
                        tranzactie.curs_valutar_bnr = ron_exchange_rate(
                            amount=1,
                            currency=valuta_cont,
                            date=tranzactie.data_transfer,
                        )

                # Adauga comisioanele bancare si convertestele in RON daca este cazul
                if (
                    "Comision Bancar".lower()
                    in tranzactie.nume_parte_din_descriere_transfer.lower()
                ):
                    if valuta_cont == "RON" and tranzactie.suma_debit:
                        tranzactie.comision_bancar_deductibil_in_ron = (
                            tranzactie.suma_debit
                        )

                    if valuta_cont != "RON" and tranzactie.suma_debit:
                        tranzactie.comision_bancar_deductibil_in_ron = (
                            ron_exchange_rate(
                                amount=tranzactie.suma_debit,
                                currency=valuta_cont,
                                date=tranzactie.data_transfer,
                            )
                        )

                # Calculeaza diferente curs valutar
                if all(
                    [
                        tranzactie.suma_valuta_schimbata,
                        tranzactie.curs_valutar_aplicat,
                        tranzactie.suma_debit,
                        valuta_cont != "RON",
                    ]
                ):
                    tranzactie.curs_valutar_bnr = ron_exchange_rate(
                        amount=1,
                        currency=valuta_cont,
                        date=tranzactie.data_transfer,
                    )

                    suma_in_ron_la_cursul_bnr = ron_exchange_rate(
                        amount=tranzactie.suma_debit,
                        currency=valuta_cont,
                        date=tranzactie.data_transfer,
                    )

                    diferenta = Decimal(str(suma_in_ron_la_cursul_bnr)) - Decimal(
                        str(tranzactie.suma_valuta_schimbata)
                    )
                    tranzactie.diferenta_curs_valutar_deductibila_in_ron = float(
                        diferenta.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    )

            self.send_message(f"Am terminat de procesat extrasul bancar '{pdf_name}'!")

            return RaspunsProcesare(
                extras_bancar=extras_bancar,
                nume_fisier_pdf=pdf_name,
            )
        except Exception as ex:
            log.exception(ex)
            return RaspunsProcesare(
                extras_bancar=None,
                eroare=True,
                nume_fisier_pdf=pdf_name,
            )

    def extract_bank_statement_generator(
        self, pdfs: list[PDFFile]
    ) -> Generator[RaspunsProcesare, None, None]:
        try:
            BATCH_SIZE = 10

            pdf_paths = [p.pdf_path for p in pdfs]

            for i in range(0, len(pdf_paths), BATCH_SIZE):
                batch = pdf_paths[i : i + BATCH_SIZE]

                results: list[RaspunsProcesare] = Parallel(
                    n_jobs=BATCH_SIZE, backend="threading"
                )(
                    delayed(self.proceseaza_pdf_extras_bancar)(pdf_path)
                    for pdf_path in batch
                )

                for raspuns_final in results:
                    for pdf in pdfs:
                        if pdf.pdf_name == raspuns_final.nume_fisier_pdf:
                            raspuns_final.numar_pagini_pdf = pdf.pdf_pages
                            break
                    yield raspuns_final

        except Exception as ex:
            log.exception(ex)
            yield RaspunsProcesare(
                extras_bancar=None,
                eroare=True,
            )

    def extract_pdf_paths_from_zip(self) -> list[str]:
        zip_path = Path(self.zip_path)

        extracted_pdfs = []
        unique_files_hashes = set()

        def process_zip(path_to_zip: Path):
            """Extract a zip, recursively scan files, process nested zip files."""
            with zipfile.ZipFile(path_to_zip, "r") as z:
                for member in z.infolist():
                    # Extract into memory
                    with z.open(member) as f:
                        data = f.read()

                    name = member.filename

                    # Normalize nested names
                    name_path = Path(name)

                    # Case 1: PDF file
                    if name_path.suffix.lower() == ".pdf":
                        fhash = hashlib.sha256(data).hexdigest()[:16]

                        if fhash in unique_files_hashes:
                            continue

                        unique_files_hashes.add(fhash)

                        out_path = Path(self.shared_dir).joinpath(fhash)
                        out_path.write_bytes(data)

                        extracted_pdfs.append(str(out_path))
                        continue

                    # Case 2: Nested zip
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

        # Remove duplicates
        extracted_pdfs = list(set(extracted_pdfs))

        return extracted_pdfs

    def is_bank_statement(self, text: str):
        if not text:
            return False

        text = unidecode(str(text)).lower()

        has_statement = "statement" in text or "extras" in text
        has_debit_credit = "debit" in text and "credit" in text
        has_balance = "balance" in text or "sold" in text

        if all([has_statement, has_debit_credit, has_balance]):
            return True

        return False

    def get_bank_statement_pdfs(self) -> list[PDFFile]:
        extracted_pdfs = self.extract_pdf_paths_from_zip()

        pdfs = []
        unique_statements = set()
        for pdf_path in extracted_pdfs:
            reader = PdfReader(pdf_path)
            number_of_pages = len(reader.pages)
            # Check if bank statement
            page = reader.pages[0]
            text = page.extract_text()

            if not self.is_bank_statement(text):
                continue

            fhash = hashlib.sha256(text.encode("utf-8")).hexdigest()

            if fhash in unique_statements:
                continue

            unique_statements.add(fhash)

            src = Path(pdf_path)
            filename = f"{src.name}.pdf"
            dst = self.extrase_bancare_path / filename
            src.rename(dst)

            pdf = PDFFile(
                pdf_name=filename,
                pdf_path=str(dst),
                pdf_pages=number_of_pages,
            )

            pdfs.append(pdf)

        return pdfs

    def extract_data_from_zip_generator(
        self, pdfs: list[PDFFile]
    ) -> Generator[RaspunsProcesare, None, None]:
        try:
            if pdfs:
                for response in self.extract_bank_statement_generator(pdfs):
                    yield response
            else:
                log.warning("Nu am putut extrage extrasele bancare in format PDF")
                yield RaspunsProcesare(
                    extras_bancar=None,
                    eroare=True,
                )
        except Exception as ex:
            log.exception(ex)
            yield RaspunsProcesare(
                extras_bancar=None,
                eroare=True,
            )

    def process_zip(self):
        try:
            if self.pdfs is None:
                pdfs = self.get_bank_statement_pdfs()
            else:
                pdfs = self.pdfs

            if len(pdfs) == 0:
                log.info("Nici un extras bancar gasit.")
                return

            pdf_data_generator = self.extract_data_from_zip_generator(pdfs)

            total_pagini_pdf = 0

            rows: list[ExtrasBancarLineSchema] = []
            for data in pdf_data_generator:
                if data.eroare or not data.extras_bancar or not data.extras_bancar.tranzactii:
                    continue

                # Rename extras_bancar
                # ex: extras_2025_05_31_e2a4e457a007b38a.pdf
                last_date = max(
                    item.data_transfer for item in data.extras_bancar.tranzactii
                )
                nume_fisier_pdf_new = (
                    "extras_"
                    + last_date.isoformat().replace("-", "_")[:7]  # YYYY_MM
                    + "_"
                    + data.extras_bancar.proprietar_cont_bancar.valuta_cont
                    + "_"
                    + data.nume_fisier_pdf
                )

                src = self.extrase_bancare_path / Path(data.nume_fisier_pdf)
                dst = self.extrase_bancare_path / Path(nume_fisier_pdf_new)
                src.rename(dst)

                data.nume_fisier_pdf = nume_fisier_pdf_new

                # Calculate all
                total_pagini_pdf += data.numar_pagini_pdf

                # Create rows
                for item in data.extras_bancar.tranzactii:
                    row = ExtrasBancarLineSchema(
                        nume_fisier=data.nume_fisier_pdf,
                        # Date cont
                        nume_cont=data.extras_bancar.proprietar_cont_bancar.nume_detinator_cont,
                        denumire_cont=data.extras_bancar.proprietar_cont_bancar.denumire_cont,
                        valuta_cont=data.extras_bancar.proprietar_cont_bancar.valuta_cont,
                        iban=data.extras_bancar.proprietar_cont_bancar.iban_cont,
                        # Detalii Tranzacție
                        data=item.data_transfer,
                        descriere=item.descriere_transfer,
                        nume_extras=item.nume_parte_din_descriere_transfer,
                        iban_extras=item.iban_din_descriere_transfer,
                        transfer_intern=item.transfer_intern_intre_conturi,
                        # Sume
                        debit=item.suma_debit,
                        credit=item.suma_credit,
                        debit_ron=item.suma_debit_in_ron,
                        credit_ron=item.suma_credit_in_ron,
                        # Detalii Schimb Valutar (dacă există)
                        valuta_schimbata=item.suma_valuta_schimbata,
                        curs_banca=item.curs_valutar_aplicat,
                        curs_bnr=item.curs_valutar_bnr,
                        diferenta_curs_valutar=item.diferenta_curs_valutar_deductibila_in_ron,
                        comision_bancar=item.comision_bancar_deductibil_in_ron,
                    )
                    rows.append(row)

            # Sort from newest to oldest by date
            rows = sorted(rows, key=lambda r: r.data, reverse=True)
            rows_dict = [r.model_dump() for r in rows]

            # Create reports
            create_excel(
                rows_dict,
                output_path=f"{self.report_path}/raport_extrase_bancare.xlsx",
            )

            report_dir = Path(self.report_path)
            shared_dir = Path(self.shared_dir)

            zip_name = f"raport_extrase_bancare_{uuid.uuid4().hex}.zip"
            zip_path = shared_dir / zip_name

            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
                for file_path in report_dir.rglob("*"):
                    if file_path.is_file():
                        # keep paths relative to report_dir
                        z.write(file_path, arcname=file_path.relative_to(report_dir))

            return RaspunsRaport(
                zip_path=str(zip_path),
                total_documente=total_pagini_pdf,
            )
        except Exception as ex:
            log.exception(ex)
            raise

    def cleanup(self):
        safe_rmtree(self.shared_dir)
