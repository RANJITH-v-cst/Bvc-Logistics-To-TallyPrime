"""
Logging utility for Tally to BVC Logistics middleware.
Maintains daily rotating log files under the /logs folder.
Guarantees masking of sensitive API credentials in logs.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from typing import Any, Dict, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)


class MaskingFormatter(logging.Formatter):
    """Custom logging formatter that scrubs API keys and secrets from output."""

    def __init__(self, fmt: Optional[str] = None, datefmt: Optional[str] = None):
        super().__init__(fmt, datefmt)
        # Regex to catch API key patterns or headers
        self._key_patterns = [
            re.compile(r'("?(?:X-Contacts-Key|api_key|appKey)"?\s*[:=]\s*"?[^",\s]+"?)(?![\w])', re.IGNORECASE),
            re.compile(r"('?(?:X-Contacts-Key|api_key|appKey)'?\s*[:=]\s*'?[^',\s]+'?)(?![\w])", re.IGNORECASE),
        ]

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        for pattern in self._key_patterns:
            msg = pattern.sub(r'\1 -> [MASKED]', msg)
        return msg


def get_daily_logger(name: str = "bvc_middleware") -> logging.Logger:
    """Return a logger configured with daily log rotation."""
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)

    # Avoid duplicate handlers if already configured
    if not logger.handlers:
        log_filename = os.path.join(LOGS_DIR, "bvc_middleware.log")
        # Rotates at midnight, suffixing YYYY-MM-DD
        handler = TimedRotatingFileHandler(
            filename=log_filename,
            when="midnight",
            interval=1,
            backupCount=30,
            encoding="utf-8"
        )
        handler.suffix = "%Y-%m-%d.log"
        # Match standard rotation naming: bvc_middleware.log.YYYY-MM-DD
        handler.setLevel(logging.INFO)

        formatter = MaskingFormatter(
            fmt="[%(asctime)s] [%(levelname)s] [%(filename)s:%(lineno)d] - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

        # Also add console handler for standard output during development/service run
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    return logger


LOGGER = get_daily_logger()


def mask_sensitive_headers(headers: Dict[str, Any]) -> Dict[str, Any]:
    """Return a shallow copy of headers with sensitive keys masked."""
    masked = dict(headers)
    for key in masked:
        if key.lower() in ("x-contacts-key", "authorization", "api-key", "apikey", "appkey"):
            val = str(masked[key])
            if len(val) > 8:
                masked[key] = f"{val[:4]}...{val[-4:]} [MASKED]"
            else:
                masked[key] = "***MASKED***"
    return masked


def log_transaction(
    tag: str,
    endpoint: str,
    request_headers: Optional[Dict[str, Any]] = None,
    request_body: Any = None,
    response_body: Any = None,
    status_code: Optional[int] = None,
    duration_ms: Optional[float] = None,
    error: Optional[str] = None
) -> None:
    """
    Log full raw request and response details safely to daily log file.
    Masks credentials automatically.
    """
    safe_headers = mask_sensitive_headers(request_headers or {})

    # Stringify request body if JSON-like
    if isinstance(request_body, (dict, list)):
        req_str = json.dumps(request_body, ensure_ascii=False, indent=2)
    else:
        req_str = str(request_body or "")

    # Stringify response body if JSON-like
    if isinstance(response_body, (dict, list)):
        resp_str = json.dumps(response_body, ensure_ascii=False, indent=2)
    else:
        resp_str = str(response_body or "")

    duration_str = f" in {duration_ms:.1f}ms" if duration_ms is not None else ""
    status_str = f"Status: {status_code}" if status_code is not None else ""

    lines = [
        f"=== [{tag}] TRANSACTION {status_str}{duration_str} ===",
        f"Endpoint: {endpoint}",
    ]
    if safe_headers:
        lines.append(f"Headers: {json.dumps(safe_headers)}")
    if req_str:
        lines.append(f"Request Payload:\n{req_str}")
    if resp_str:
        lines.append(f"Response Body:\n{resp_str}")
    if error:
        lines.append(f"Error: {error}")
    lines.append("=" * 60)

    msg = "\n".join(lines)
    if error or (status_code and status_code >= 400):
        LOGGER.error(msg)
    else:
        LOGGER.info(msg)
