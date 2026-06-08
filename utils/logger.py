"""
utils/logger.py — Structured logger for Delta X
"""
import logging
import sys
from config.settings import LOG_LEVEL, SYSTEM_NAME


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f"{SYSTEM_NAME}.{name}")

    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)
    logger.propagate = False
    return logger
