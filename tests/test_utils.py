from __future__ import annotations

from pathlib import Path

import pandas as pd

from pfasimplu_cli.utils import create_excel


def test_create_excel_handles_missing_values(tmp_path: Path) -> None:
    data = [
        {"vanzator_cui_tva": "RO6529540", "total_de_plata": 10.5},
        {"vanzator_cui_tva": None, "total_de_plata": None},
        {"vanzator_cui_tva": None, "total_de_plata": 3.0},
    ]

    output = Path(create_excel(data, tmp_path / "raport.xlsx"))

    assert output.exists()
    assert len(pd.read_excel(output)) == 3


def test_create_excel_handles_all_missing_values(tmp_path: Path) -> None:
    data = [{"vanzator_cui_tva": None}, {"vanzator_cui_tva": None}]

    output = Path(create_excel(data, tmp_path / "raport.xlsx"))

    assert output.exists()
