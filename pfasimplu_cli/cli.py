"""Typer command line interface for the standalone PFA processor."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer
from loguru import logger as log

from pfasimplu_cli import __version__
from pfasimplu_cli.config import (
    DEFAULT_CONFIG_NAME,
    DEFAULT_MODEL,
    AppConfig,
    CompanyConfig,
    ConfigError,
    GeminiConfig,
    ProcessingConfig,
    config_to_dict,
    find_config_path,
    load_config,
    write_config,
)
from pfasimplu_cli.job import JobError, run_job
from pfasimplu_cli.logging import setup_logging

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Generate Romanian PFA fiscal registers from a ZIP of documents.",
)
config_app = typer.Typer(no_args_is_help=True, help="Inspect the local configuration.")
app.add_typer(config_app, name="config")

ENV_FILE = ".env"


def _cwd_config_path() -> Path:
    return Path.cwd() / DEFAULT_CONFIG_NAME


def _load_env_files(config_path: Path | None = None) -> None:
    """Load .env from the current directory and from the config directory."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - python-dotenv is a declared dependency
        return

    load_dotenv(Path.cwd() / ENV_FILE, override=False)
    if config_path and config_path.parent != Path.cwd():
        load_dotenv(config_path.parent / ENV_FILE, override=False)


def _questionnaire(existing: AppConfig | None = None) -> AppConfig:
    typer.secho(
        "\nPrima configurare pfasimplu. Raspunde la cateva intrebari; "
        "raspunsurile se salveaza in pfasimplu.toml.",
        fg=typer.colors.CYAN,
        bold=True,
    )

    company = existing.company if existing else None
    processing = existing.processing if existing else ProcessingConfig()
    gemini = existing.gemini if existing else GeminiConfig()

    nume_firma = typer.prompt(
        "Numele firmei / PFA",
        default=company.nume_firma if company else None,
    ).strip()
    cui_cif = typer.prompt(
        "CUI / CIF",
        default=company.cui_cif if company else None,
    ).strip()
    cod_tva = typer.prompt(
        "Cod TVA (optional, ENTER pentru a sari)",
        default=(company.cod_tva if company and company.cod_tva else "-"),
        show_default=False,
    ).strip()
    if cod_tva == "-":
        cod_tva = ""
    model = typer.prompt(
        "Model Gemini",
        default=processing.model or DEFAULT_MODEL,
    ).strip()
    output_dir = typer.prompt(
        "Director pentru rezultate",
        default=processing.output_dir or "./output",
    ).strip()
    api_key_env = typer.prompt(
        "Numele variabilei de environment pentru cheia Gemini",
        default=gemini.api_key_env or "GOOGLE_GENAI_KEY",
    ).strip()

    typer.secho(
        f"\nCheia Gemini se citeste din environment sau din {ENV_FILE}. Exemplu: {api_key_env}=...",
        fg=typer.colors.YELLOW,
    )

    return AppConfig(
        company=CompanyConfig(
            nume_firma=nume_firma,
            cui_cif=cui_cif,
            cod_tva=cod_tva,
        ),
        processing=ProcessingConfig(
            model=model,
            output_dir=output_dir,
            keep_workdir=processing.keep_workdir,
        ),
        gemini=GeminiConfig(api_key_env=api_key_env, api_key=gemini.api_key),
    )


def _ensure_config(config_path: Path | None) -> AppConfig:
    """Load the config, running the first-run questionnaire when needed."""
    if config_path is not None:
        _load_env_files(config_path)
        return load_config(config_path)

    cwd_config = _cwd_config_path()
    if cwd_config.exists():
        _load_env_files(cwd_config)
        return load_config(cwd_config)

    _load_env_files(None)
    if not sys.stdin.isatty():
        raise ConfigError(
            f"No {DEFAULT_CONFIG_NAME} found in {Path.cwd()}. "
            "Run `pfasimplu init` first (interactive terminal required)."
        )

    typer.secho(
        f"\nNu am gasit {DEFAULT_CONFIG_NAME} in directorul curent.",
        fg=typer.colors.YELLOW,
    )
    if not typer.confirm("Vrei sa il cream acum?", default=True):
        raise ConfigError("Configuration is required before processing.")

    config = _questionnaire()
    path = write_config(config, cwd_config)
    typer.secho(f"Configurare salvata in {path}", fg=typer.colors.GREEN)
    config.path = path
    return config


def _print_config(config: AppConfig) -> None:
    import tomli_w

    data = config_to_dict(config)
    if data.get("gemini", {}).get("api_key"):
        data["gemini"]["api_key"] = "***redacted***"
    typer.echo(tomli_w.dumps(data), nl=False)


@app.command()
def init(
    config: Optional[Path] = typer.Option(
        None, "--config", "-c", help="Where to write the TOML file."
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite an existing configuration."),
) -> None:
    """Create pfasimplu.toml with an interactive questionnaire."""
    path = (config or _cwd_config_path()).expanduser()
    _load_env_files(path)

    existing: AppConfig | None = None
    if path.exists():
        if not force:
            typer.secho(
                f"Config already exists: {path}. Use --force to overwrite.",
                fg=typer.colors.RED,
            )
            raise typer.Exit(code=1)
        try:
            existing = load_config(path)
        except ConfigError:
            existing = None

    new_config = _questionnaire(existing)
    written = write_config(new_config, path, force=True)
    typer.secho(f"Configurare salvata in {written}", fg=typer.colors.GREEN)


@app.command()
def process(
    input_zip: Path = typer.Argument(..., help="ZIP containing the PFA documents."),
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="Path to pfasimplu.toml."),
    output_dir: Optional[Path] = typer.Option(
        None, "--output-dir", "-o", help="Override the output directory."
    ),
    model: Optional[str] = typer.Option(None, "--model", help="Override the Gemini model."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging."),
    keep_workdir: bool = typer.Option(
        False, "--keep-workdir", help="Keep temporary working files for inspection."
    ),
    log_file: Optional[Path] = typer.Option(
        None, "--log-file", help="Also write logs to this file."
    ),
) -> None:
    """Process one ZIP and write the registre ZIP plus a JSON summary."""
    setup_logging(verbose=verbose, log_file=log_file)

    try:
        cfg = _ensure_config(config)
    except ConfigError as exc:
        typer.secho(f"Config error: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc

    if model:
        cfg.processing.model = model
    if output_dir:
        cfg.processing.output_dir = str(output_dir)

    try:
        api_key = cfg.get_api_key()
    except ConfigError as exc:
        typer.secho(f"Credential error: {exc}", fg=typer.colors.RED)
        typer.secho(
            f"Seteaza cheia in {ENV_FILE} sau in environment, apoi reincearca.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=2) from exc

    # Do not echo the key; just confirm it is available.
    log.debug("Gemini API key loaded ({} characters).", len(api_key))

    try:
        result = run_job(
            input_zip,
            cfg,
            output_dir=output_dir,
            keep_workdir=keep_workdir or None,
        )
    except (JobError, ConfigError) as exc:
        typer.secho(f"Processing failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    typer.secho("\nGata!", fg=typer.colors.GREEN, bold=True)
    typer.echo(f"  Registre: {result.output_zip}")
    typer.echo(f"  Sumar:    {result.output_json}")
    typer.echo(f"  Documente: {result.total_documente}")
    typer.echo(f"  Durata:   {result.duration_seconds:.1f}s")


@config_app.command("show")
def config_show(
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="Path to pfasimplu.toml."),
) -> None:
    """Print the effective configuration (API key redacted)."""
    try:
        cfg = _ensure_config(config)
    except ConfigError as exc:
        typer.secho(f"Config error: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc
    typer.echo(f"# {cfg.path}")
    _print_config(cfg)


@config_app.command("path")
def config_path() -> None:
    """Print the configuration file that would be used."""
    try:
        path = find_config_path()
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc
    typer.echo(str(path))


@app.command()
def doctor(
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="Path to pfasimplu.toml."),
    check_api: bool = typer.Option(
        False, "--check-api", help="Make one small Gemini request to verify the key."
    ),
) -> None:
    """Check configuration, credentials, dependencies and writability."""
    problems = 0

    def ok(label: str, detail: str = "") -> None:
        typer.secho(f"  OK    {label}" + (f"  {detail}" if detail else ""), fg=typer.colors.GREEN)

    def bad(label: str, detail: str) -> None:
        nonlocal problems
        problems += 1
        typer.secho(f"  FAIL  {label}  {detail}", fg=typer.colors.RED)

    typer.echo(f"Python: {sys.version.split()[0]}")
    if sys.version_info < (3, 11):
        bad("Python >= 3.11", "Upgrade Python.")
    else:
        ok("Python >= 3.11")

    for module in ("google.genai", "pydantic", "pypdf", "pandas", "openpyxl", "joblib"):
        try:
            __import__(module)
        except Exception as exc:  # noqa: BLE001
            bad(f"import {module}", str(exc))
        else:
            ok(f"import {module}")

    cfg: AppConfig | None = None
    try:
        cfg = _ensure_config(config)
        ok("config", str(cfg.path))
    except ConfigError as exc:
        bad("config", str(exc))

    if cfg:
        try:
            key = cfg.get_api_key()
            ok("Gemini API key", f"{len(key)} characters")
        except ConfigError as exc:
            bad("Gemini API key", str(exc))

        out_dir = cfg.resolve_output_dir()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            probe = out_dir / ".pfasimplu_write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            ok("output dir", str(out_dir))
        except OSError as exc:
            bad("output dir", f"{out_dir}: {exc}")

        if check_api:
            try:
                from google import genai

                client = genai.Client(api_key=cfg.get_api_key())
                client.models.generate_content(
                    model=cfg.processing.model,
                    contents="Reply with the single word: ok",
                )
                ok("Gemini request", cfg.processing.model)
            except Exception as exc:  # noqa: BLE001
                bad("Gemini request", str(exc))

    typer.echo("")
    if problems:
        typer.secho(f"{problems} problem(s) found.", fg=typer.colors.RED, bold=True)
        raise typer.Exit(code=1)
    typer.secho("Everything looks good.", fg=typer.colors.GREEN, bold=True)


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(f"pfasimplu-cli {__version__}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
