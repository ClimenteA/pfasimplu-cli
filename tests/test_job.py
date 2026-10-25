from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from pfasimplu_cli.config import AppConfig, CompanyConfig, GeminiConfig, ProcessingConfig
from pfasimplu_cli.job import JobError, run_job


class FakeAgent:
    last_instance: "FakeAgent | None" = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.shared_dir = Path(kwargs["zip_path"]).parent / "_fake_work"
        self.shared_dir.mkdir(parents=True, exist_ok=True)
        FakeAgent.last_instance = self

    def process_zip(self):
        zip_path = self.shared_dir / "registre_pfa_fake.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("registru.xlsx", b"fake")
        return SimpleNamespace(
            zip_path=str(zip_path),
            total_documente=7,
            total_documente_bonuri=1,
            total_documente_facturi=2,
            total_documente_efacturi=3,
            total_documente_extrase=1,
        )

    def cleanup(self):
        shutil.rmtree(self.shared_dir, ignore_errors=True)


@pytest.fixture()
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    monkeypatch.setenv("GOOGLE_GENAI_KEY", "test-key")
    return AppConfig(
        company=CompanyConfig(nume_firma="PFA Test", cui_cif="1234567890"),
        processing=ProcessingConfig(output_dir=str(tmp_path / "out")),
        gemini=GeminiConfig(),
    )


@pytest.fixture()
def input_zip(tmp_path: Path) -> Path:
    path = tmp_path / "documente.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("readme.txt", "hello")
    return path


def test_rejects_missing_input(config: AppConfig, tmp_path: Path) -> None:
    with pytest.raises(JobError):
        run_job(tmp_path / "missing.zip", config)


def test_rejects_non_zip_input(config: AppConfig, tmp_path: Path) -> None:
    path = tmp_path / "not-a-zip.txt"
    path.write_text("x", encoding="utf-8")
    with pytest.raises(JobError):
        run_job(path, config)


def test_run_job_moves_output_and_keeps_input(
    config: AppConfig, input_zip: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("pfasimplu_cli.job.AgentRegistrePFA", FakeAgent)
    monkeypatch.setattr(
        "pfasimplu_cli.job.genai.Client",
        lambda **kwargs: SimpleNamespace(kwargs=kwargs),
    )

    result = run_job(input_zip, config)

    assert result.success is True
    assert result.total_documente == 7
    assert result.total_documente_bonuri == 1
    assert result.total_documente_facturi == 2
    assert result.total_documente_efacturi == 3
    assert result.total_documente_extrase == 1

    assert input_zip.exists(), "the user input ZIP must never be deleted"
    assert result.output_zip is not None
    output_zip = Path(result.output_zip)
    assert output_zip.exists()
    assert output_zip.parent == (input_zip.parent / "out").resolve()
    assert output_zip.name.startswith("registre_pfa_documente_")

    assert result.output_json is not None
    summary = json.loads(Path(result.output_json).read_text(encoding="utf-8"))
    assert summary["total_documente"] == 7
    assert summary["success"] is True

    # cleanup removed the fake agent work dir
    assert not (input_zip.parent / "_fake_work").exists()
