"""Standalone, synchronous version of the Django PFA processing job.

The job takes a ZIP of PFA documents, runs every extraction agent, builds the
fiscal registers, and writes the resulting ZIP plus a small JSON summary to the
configured output directory. The input ZIP is never modified or deleted.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from google import genai
from loguru import logger as log

from pfasimplu_cli.agents.registre_pfa import AgentRegistrePFA
from pfasimplu_cli.config import AppConfig, ConfigError


class JobError(RuntimeError):
    """Raised when a processing job cannot be completed."""


@dataclass
class JobResult:
    success: bool
    input_zip: str
    output_zip: str | None
    output_json: str | None
    total_documente: int
    total_documente_bonuri: int
    total_documente_facturi: int
    total_documente_efacturi: int
    total_documente_extrase: int
    duration_seconds: float
    started_at: str
    finished_at: str
    error: str | None = None


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _safe_stem(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)
    return cleaned.strip("._-") or "documente"


def _validate_input(input_zip: str | Path) -> Path:
    path = Path(input_zip).expanduser()
    if not path.exists():
        raise JobError(f"Input ZIP not found: {path}")
    if not path.is_file():
        raise JobError(f"Input path is not a file: {path}")
    if path.suffix.lower() != ".zip":
        raise JobError(f"Input must be a ZIP file, got: {path.name}")
    return path.resolve()


def run_job(
    input_zip: str | Path,
    config: AppConfig,
    *,
    output_dir: str | Path | None = None,
    keep_workdir: bool | None = None,
) -> JobResult:
    """Process one input ZIP and return a serializable result.

    Raises ``JobError`` if the input is invalid or the processing agents fail.
    """
    input_path = _validate_input(input_zip)
    started = datetime.now()
    started_monotonic = time.monotonic()
    run_stamp = _timestamp()
    stem = _safe_stem(input_path.stem)

    out_dir = Path(output_dir).expanduser() if output_dir else config.resolve_output_dir()
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    api_key = config.get_api_key()
    client = genai.Client(api_key=api_key)
    keep = config.processing.keep_workdir if keep_workdir is None else keep_workdir

    log.info("Input ZIP:  {}", input_path)
    log.info("Output dir: {}", out_dir)
    log.info("Company:    {} (CUI {})", config.company.nume_firma, config.company.cui_cif)
    log.info("Gemini:     {}", config.processing.model)

    agent = AgentRegistrePFA(
        zip_path=str(input_path),
        nume_firma=config.company.nume_firma,
        cui_cif=config.company.cui_cif,
        cod_tva=config.company.cod_tva,
        model=config.processing.model,
        client=client,
    )

    result: JobResult | None = None
    error: str | None = None
    try:
        log.info("[1/4] Procesare documente si construire registre...")
        response = agent.process_zip()
        if response is None:
            raise JobError(
                "Processing returned no result. Check the logs above for the failing agent."
            )

        log.info("[2/4] Mutare arhivei finale in directorul de rezultate...")
        produced = Path(response.zip_path)
        if not produced.exists():
            raise JobError(f"Agent did not produce the expected ZIP: {produced}")

        final_name = f"registre_pfa_{stem}_{run_stamp}.zip"
        output_zip = out_dir / final_name
        shutil.move(str(produced), output_zip)

        log.info("[3/4] Scriere sumar JSON...")
        result = JobResult(
            success=True,
            input_zip=str(input_path),
            output_zip=str(output_zip),
            output_json=None,
            total_documente=response.total_documente,
            total_documente_bonuri=response.total_documente_bonuri,
            total_documente_facturi=response.total_documente_facturi,
            total_documente_efacturi=response.total_documente_efacturi,
            total_documente_extrase=response.total_documente_extrase,
            duration_seconds=round(time.monotonic() - started_monotonic, 2),
            started_at=started.isoformat(timespec="seconds"),
            finished_at=datetime.now().isoformat(timespec="seconds"),
        )

        json_path = out_dir / f"{output_zip.stem}.json"
        result.output_json = str(json_path)
        json_path.write_text(
            json.dumps(asdict(result), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        log.info("[4/4] Rezultat: {}", output_zip)
        log.info(
            "Documente: {} (bonuri {}, facturi {}, e-facturi {}, extrase {})",
            result.total_documente,
            result.total_documente_bonuri,
            result.total_documente_facturi,
            result.total_documente_efacturi,
            result.total_documente_extrase,
        )
        log.info("Durata: {:.1f}s", result.duration_seconds)
        return result

    except ConfigError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface a single readable job error
        error = str(exc)
        log.exception("Job failed: {}", exc)
        result = JobResult(
            success=False,
            input_zip=str(input_path),
            output_zip=None,
            output_json=None,
            total_documente=0,
            total_documente_bonuri=0,
            total_documente_facturi=0,
            total_documente_efacturi=0,
            total_documente_extrase=0,
            duration_seconds=round(time.monotonic() - started_monotonic, 2),
            started_at=started.isoformat(timespec="seconds"),
            finished_at=datetime.now().isoformat(timespec="seconds"),
            error=error,
        )
        json_path = out_dir / f"registre_pfa_{stem}_{run_stamp}_error.json"
        result.output_json = str(json_path)
        json_path.write_text(
            json.dumps(asdict(result), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        raise JobError(error) from exc

    finally:
        if keep:
            log.info("Work directory kept at: {}", agent.shared_dir)
        else:
            agent.cleanup()
