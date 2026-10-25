from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Optional

from google import genai
from google.genai import types
from loguru import logger
from pydantic import BaseModel, model_validator
from pypdf import PdfReader
from unidecode import unidecode

from pfasimplu_cli.utils import get_mime_type_from_file

TEXT_EXTRACTION_PROMPT = (
    "Extract all visible text from this document, preserving line breaks and table "
    "structure as much as possible. Return only the extracted text, without comments, "
    "markdown fences, or explanations."
)


class ExtractedTextResponse(BaseModel):
    text: Optional[str] = None
    text_hash: Optional[str] = None
    filepath: str
    error: Optional[bool] = False

    @model_validator(mode="after")
    def post_process(self):
        if self.text:
            self.text = unidecode(self.text)
            self.text_hash = uuid.uuid5(
                uuid.NAMESPACE_URL,
                hashlib.sha256(self.text.encode("utf-8")).hexdigest(),
            ).hex
        return self


def _extract_pdf_text(filepath: Path) -> str:
    reader = PdfReader(str(filepath))
    parts: list[str] = []
    for page in reader.pages:
        try:
            text = page.extract_text()
        except Exception:
            text = None
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _gemini_extract_text(
    filepath: Path,
    *,
    client: genai.Client,
    model: str,
) -> str:
    mime_type = get_mime_type_from_file(filepath)
    response = client.models.generate_content(
        model=model,
        contents=[
            types.Part.from_bytes(data=filepath.read_bytes(), mime_type=mime_type),
            TEXT_EXTRACTION_PROMPT,
        ],
        config=types.GenerateContentConfig(temperature=0),
    )
    return (response.text or "").strip()


def extract_text(
    filepath: str,
    *,
    client: genai.Client,
    model: str,
    min_pdf_chars: int = 40,
) -> ExtractedTextResponse:
    """Extract text locally from digital PDFs, falling back to Gemini vision.

    Digital PDFs are read with pypdf (no network). Scanned PDFs and images are sent
    to Gemini, replacing the original Google DocumentAI dependency.
    """
    path = Path(filepath)
    try:
        if path.suffix.lower() == ".pdf":
            text = _extract_pdf_text(path)
            if len(text) >= min_pdf_chars:
                return ExtractedTextResponse(text=text, filepath=str(path))

        text = _gemini_extract_text(path, client=client, model=model)
        return ExtractedTextResponse(text=text, filepath=str(path))
    except Exception as exc:  # noqa: BLE001 - a single bad file must not kill the job
        logger.exception("Could not extract text from {}: {}", path, exc)
        return ExtractedTextResponse(filepath=str(path), error=True)
