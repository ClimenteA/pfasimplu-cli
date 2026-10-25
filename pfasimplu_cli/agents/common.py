import hashlib
from enum import StrEnum

import numpy as np
import pandas as pd
from pydantic import BaseModel


class ModalitatePlata(StrEnum):
    CARD = "CARD"
    NUMERAR = "NUMERAR"


class RaspunsRaport(BaseModel):
    zip_path: str
    total_documente: int


def get_data_from_excel(excel_path: str):
    df = pd.read_excel(excel_path, dtype=str)
    df = df.replace({pd.NA: None, np.nan: None, "nan": None, "NaN": None})
    datali = df.to_dict(orient="records")
    return datali


def get_model_hash(model: BaseModel) -> str:
    model_data = model.model_dump_json().encode("utf-8")
    return hashlib.sha256(model_data).hexdigest()
