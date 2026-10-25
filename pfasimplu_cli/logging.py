from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger


def setup_logging(*, verbose: bool = False, log_file: str | Path | None = None) -> None:
    """Configure loguru for the CLI.

    Logs go to stderr so stdout stays clean for shell pipelines.
    """
    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG" if verbose else "INFO",
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>",
    )
    if log_file:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        logger.add(path, level="DEBUG", rotation="10 MB", retention=5, encoding="utf-8")
