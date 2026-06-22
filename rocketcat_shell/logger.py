from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


LOGGER_NAME = "rocketcat"
logger = logging.getLogger(LOGGER_NAME)


def configure_logging(
    log_file: Path,
    *,
    level_name: str = "INFO",
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 3,
) -> None:
    level = getattr(logging, str(level_name or "INFO").upper(), logging.INFO)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        fmt="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=max(1, int(max_bytes)),
        backupCount=max(0, int(backup_count)),
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)

    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    logger.setLevel(level)
