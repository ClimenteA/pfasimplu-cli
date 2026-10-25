from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w
from dotenv import load_dotenv

DEFAULT_CONFIG_NAME = "pfasimplu.toml"
DEFAULT_MODEL = "gemini-3.8-flash"


class ConfigError(RuntimeError):
    """Raised when the local TOML configuration is missing or invalid."""


@dataclass
class CompanyConfig:
    nume_firma: str
    cui_cif: str
    cod_tva: str = ""


@dataclass
class ProcessingConfig:
    model: str = DEFAULT_MODEL
    output_dir: str = "./output"
    keep_workdir: bool = False


@dataclass
class GeminiConfig:
    api_key: str | None = None


@dataclass
class AppConfig:
    company: CompanyConfig
    processing: ProcessingConfig = field(default_factory=ProcessingConfig)
    gemini: GeminiConfig = field(default_factory=GeminiConfig)
    path: Path | None = None

    def get_api_key(self) -> str:
        """Return the Gemini API key from GOOGLE_GENAI_KEY or the TOML file."""
        key = os.getenv("GOOGLE_GENAI_KEY") or self.gemini.api_key
        if not key:
            raise ConfigError(
                "Gemini API key not found. Set GOOGLE_GENAI_KEY in your "
                "environment/.env, or add [gemini].api_key to pfasimplu.toml."
            )
        return key

    @property
    def base_dir(self) -> Path:
        return self.path.parent if self.path else Path.cwd()

    def resolve_output_dir(self) -> Path:
        return _resolve_path(self.processing.output_dir, self.base_dir)


def _resolve_path(value: str | Path, base: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def find_config_path(explicit: str | Path | None = None) -> Path:
    """Find a config path from an explicit value, env var, or cwd."""
    load_dotenv()
    if explicit:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise ConfigError(f"Config file not found: {path}")
        load_dotenv(path.parent / ".env", override=False)
        return path.resolve()

    env_path = os.getenv("PFASIMPLU_CONFIG")
    if env_path:
        path = Path(env_path).expanduser()
        if not path.exists():
            raise ConfigError(f"Config file not found (PFASIMPLU_CONFIG): {path}")
        load_dotenv(path.parent / ".env", override=False)
        return path.resolve()

    path = Path.cwd() / DEFAULT_CONFIG_NAME
    if path.exists():
        load_dotenv(path.parent / ".env", override=False)
        return path.resolve()

    raise ConfigError(
        f"No {DEFAULT_CONFIG_NAME} found in {Path.cwd()}. "
        "Run `pfasimplu init` first or pass --config."
    )


def load_config(explicit: str | Path | None = None) -> AppConfig:
    path = find_config_path(explicit)
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc

    company_raw = raw.get("company")
    if not isinstance(company_raw, dict):
        raise ConfigError(f"{path} is missing a [company] section.")

    nume_firma = str(company_raw.get("nume_firma") or "").strip()
    cui_cif = str(company_raw.get("cui_cif") or "").strip()
    if not nume_firma:
        raise ConfigError(f"{path}: [company].nume_firma is required.")
    if not cui_cif:
        raise ConfigError(f"{path}: [company].cui_cif is required.")

    company = CompanyConfig(
        nume_firma=nume_firma,
        cui_cif=cui_cif,
        cod_tva=str(company_raw.get("cod_tva") or "").strip(),
    )

    processing_raw = raw.get("processing") or {}
    processing = ProcessingConfig(
        model=str(processing_raw.get("model") or DEFAULT_MODEL),
        output_dir=str(processing_raw.get("output_dir") or "./output"),
        keep_workdir=bool(processing_raw.get("keep_workdir", False)),
    )

    gemini_raw = raw.get("gemini") or {}
    gemini = GeminiConfig(api_key=gemini_raw.get("api_key"))

    return AppConfig(
        company=company,
        processing=processing,
        gemini=gemini,
        path=path,
    )


def config_to_dict(config: AppConfig) -> dict[str, Any]:
    processing: dict[str, Any] = {
        "model": config.processing.model,
        "output_dir": config.processing.output_dir,
        "keep_workdir": config.processing.keep_workdir,
    }

    gemini: dict[str, Any] = {}
    if config.gemini.api_key:
        gemini["api_key"] = config.gemini.api_key

    return {
        "company": {
            "nume_firma": config.company.nume_firma,
            "cui_cif": config.company.cui_cif,
            "cod_tva": config.company.cod_tva,
        },
        "processing": processing,
        "gemini": gemini,
    }


def write_config(config: AppConfig, path: str | Path, *, force: bool = False) -> Path:
    target = Path(path).expanduser()
    if target.exists() and not force:
        raise ConfigError(f"Config already exists: {target}. Use --force to overwrite.")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as fh:
        tomli_w.dump(config_to_dict(config), fh)
    return target.resolve()
