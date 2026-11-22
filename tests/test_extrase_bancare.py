from __future__ import annotations

import pytest

from pfasimplu_cli.agents.extrase_bancare import este_iban_plata_tva


@pytest.mark.parametrize(
    "iban",
    [
        "RO12TREZ70020A100101XTVA",
        "RO12 TREZ 700 20A1 0010 1XTVA",
        "ro12trez70020a100101xtva",
        "Plata TVA RO12TREZ70020A100101XTVA conform OP 123",
        "RO12TREZ7005069XXX20A100101XTVA",
    ],
)
def test_este_iban_plata_tva_pozitiv(iban: str) -> None:
    assert este_iban_plata_tva(iban) is True


@pytest.mark.parametrize(
    "iban",
    [
        None,
        "",
        "RO04INGB0000999911194833",
        "GB70REVO00996974413902",
        # cont Trezorerie, dar fara segmentul de TVA
        "RO86TREZ7005069XXX006407",
        # fara RO
        "GB12TREZ70020A100101XTVA",
        # doar 2 cifre dupa TREZ
        "RO12TREZ7020A100101XTVA",
        # fara 20A100101
        "RO12TREZ7005069XXXXTVA",
        # nu se termina in XTVA
        "RO12TREZ70020A100101XTVA1",
        "RO12TREZ70020A100101XTV",
    ],
)
def test_este_iban_plata_tva_negativ(iban: str | None) -> None:
    assert este_iban_plata_tva(iban) is False
