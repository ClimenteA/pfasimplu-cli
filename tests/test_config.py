from __future__ import annotations

from pathlib import Path

import pytest

from pfasimplu_cli.config import (
    AppConfig,
    CompanyConfig,
    ConfigError,
    GeminiConfig,
    ProcessingConfig,
    config_to_dict,
    load_config,
    write_config,
)


def make_config() -> AppConfig:
    return AppConfig(
        company=CompanyConfig(
            nume_firma="PFA Test",
            cui_cif="1234567890",
            cod_tva="RO1234567890",
        ),
        processing=ProcessingConfig(
            model="gemini-3.8-flash",
            output_dir="./out",
            keep_workdir=False,
        ),
        gemini=GeminiConfig(api_key_env="GOOGLE_GENAI_KEY"),
    )


def test_write_and_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "pfasimplu.toml"
    written = write_config(make_config(), path)
    assert written == path.resolve()

    loaded = load_config(path)
    assert loaded.company.nume_firma == "PFA Test"
    assert loaded.company.cui_cif == "1234567890"
    assert loaded.company.cod_tva == "RO1234567890"
    assert loaded.processing.model == "gemini-3.8-flash"
    assert loaded.resolve_output_dir() == (tmp_path / "out").resolve()


def test_write_refuses_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "pfasimplu.toml"
    write_config(make_config(), path)
    with pytest.raises(ConfigError):
        write_config(make_config(), path)


def test_missing_required_company_fields(tmp_path: Path) -> None:
    path = tmp_path / "pfasimplu.toml"
    path.write_text('[company]\nnume_firma = ""\ncui_cif = ""\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_api_key_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_GENAI_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    config = make_config()
    monkeypatch.setenv("GEMINI_API_KEY", "from-gemini")
    assert config.get_api_key() == "from-gemini"

    monkeypatch.setenv("GOOGLE_GENAI_KEY", "from-google")
    assert config.get_api_key() == "from-google"


def test_api_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_GENAI_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ConfigError):
        make_config().get_api_key()


def test_config_to_dict_contains_sections() -> None:
    data = config_to_dict(make_config())
    assert set(data) == {"company", "processing", "gemini"}
    assert data["company"]["nume_firma"] == "PFA Test"
