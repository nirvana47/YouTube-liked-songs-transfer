#!/usr/bin/env python3
"""Qt-free error handling, run logging and retry classification helpers.

Everything here is deliberately independent of PySide6 so it can be unit-tested
headlessly and reused by both the GUI workers and the automated scenario tests.

* ``friendly_error(exc)`` — turns any exception into a short, non-technical,
  reassuring message. The UI shows only these; full tracebacks go to the log.
* ``setup_run_logger()`` — a per-run file logger writing to
  ``logs/transfer-YYYYMMDD-HHMMSS.log`` (relative to this file's directory).
* ``classify_retryable(exc)`` — ``True`` for rate-limit *and* transient network
  errors so callers know a retry-with-backoff is worthwhile.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

# Directory that holds this module (and its sibling scripts). All runtime output
# is written relative to here so the files are copy-paste-ready for the repo.
_BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = _BASE_DIR / "logs"

_SEE_LOG = "(see the log file for details)"


def _status_code(exc: BaseException) -> Optional[int]:
    """Best-effort extraction of an HTTP status code from an exception."""
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    response = getattr(exc, "response", None)
    response_code = getattr(response, "status_code", None)
    if isinstance(response_code, int):
        return response_code
    return None


def _is_network_error(exc: BaseException) -> bool:
    """True for timeouts / connection errors (requests or stdlib)."""
    # Prefer name-based detection so we do not hard-depend on requests being
    # importable at call time.
    names = {type(exc).__name__ for exc in _exc_chain(exc)}
    network_names = {
        "Timeout",
        "ConnectTimeout",
        "ReadTimeout",
        "ConnectionError",
        "ConnectionResetError",
        "ConnectTimeoutError",
        "NewConnectionError",
        "MaxRetryError",
        "socket.timeout",
        "TimeoutError",
        "SSLError",
    }
    if names & network_names:
        return True
    # Also treat the builtin ConnectionError family and TimeoutError as network.
    for item in _exc_chain(exc):
        if isinstance(item, (ConnectionError, TimeoutError)):
            return True
    return False


def _exc_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def is_rate_limit_error(exc: BaseException) -> bool:
    """True if the exception looks like an HTTP 429 rate-limit response."""
    if _status_code(exc) == 429:
        return True
    text = repr(exc).lower()
    return "429" in text or "too many requests" in text or "rate limit" in text


def classify_retryable(exc: BaseException) -> bool:
    """Return True for errors worth retrying: rate limits and transient network."""
    return is_rate_limit_error(exc) or _is_network_error(exc)


def _is_auth_error(exc: BaseException) -> bool:
    code = _status_code(exc)
    if code in (401, 403):
        return True
    names = {type(item).__name__ for item in _exc_chain(exc)}
    if names & {"BadOAuthClient", "UnauthorizedOAuthClient"}:
        return True
    text = " ".join(str(item) for item in _exc_chain(exc)).lower()
    markers = (
        "unauthorized",
        "invalid_grant",
        "bad oauth",
        "authentication",
        "please provide authentication",
        "not authenticated",
        "401",
        "403",
    )
    return any(marker in text for marker in markers)


def friendly_error(exc: BaseException) -> str:
    """Map an exception to a short, human-readable, non-technical message."""
    # Network problems (timeout / connection dropped).
    if _is_network_error(exc):
        return (
            "Couldn't reach YouTube Music — the connection timed out or dropped. "
            "Check your internet and try again " + _SEE_LOG + "."
        )

    # Rate limiting.
    if is_rate_limit_error(exc):
        return (
            "YouTube Music is temporarily rate-limiting requests. "
            "The app will wait and retry automatically " + _SEE_LOG + "."
        )

    # Authentication / OAuth problems.
    if _is_auth_error(exc):
        return (
            "YouTube Music refused the sign-in for this account. "
            "Please sign in again from Step 1 " + _SEE_LOG + "."
        )

    # Missing files.
    if isinstance(exc, FileNotFoundError):
        return (
            "A required sign-in file is missing. "
            "Please sign in again from Step 1 " + _SEE_LOG + "."
        )

    # Parsing / unexpected shapes.
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        names = {type(item).__name__ for item in _exc_chain(exc)}
        if "JSONDecodeError" in names or isinstance(exc, (KeyError, ValueError, TypeError)):
            return (
                "YouTube Music sent back an unexpected response. "
                "This is usually temporary — please try again " + _SEE_LOG + "."
            )

    # SystemExit from core helpers carries a user-facing string already.
    if isinstance(exc, SystemExit):
        message = str(exc.code) if exc.code is not None else ""
        message = " ".join(message.split())
        if message:
            return f"{message} {_SEE_LOG}."

    # Generic fallback.
    return "Something went wrong while transferring your songs " + _SEE_LOG + "."


def setup_run_logger(name: str = "lstransfer") -> tuple[logging.Logger, Path]:
    """Create a per-run file logger. Returns ``(logger, log_path)``.

    Writes to ``logs/transfer-YYYYMMDD-HHMMSS.log`` (relative directory, created
    on demand). Each call creates a fresh log file so runs are easy to find.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = LOG_DIR / f"transfer-{timestamp}.log"

    logger = logging.getLogger(f"{name}.{timestamp}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    # Avoid duplicate handlers if a logger name is somehow reused.
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    logger.info("=== Run started ===")
    return logger, log_path
