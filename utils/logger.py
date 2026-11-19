"""
统一日志配置。
"""
from __future__ import annotations

import logging
import re

from config.settings import LOG_DIR

_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|authorization|cookie|password|client[_-]?secret|token)"
    r"(\s*[:=]\s*)([^,\s}]+)"
)


def redact(value: object) -> str:
    text = str(value)
    return _SECRET_RE.sub(r"\1\2***REDACTED***", text)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logger(name: str = "MusicAI") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = RedactingFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    fh = logging.FileHandler(LOG_DIR / "musicai.log", encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger
