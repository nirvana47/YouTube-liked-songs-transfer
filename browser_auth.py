#!/usr/bin/env python3
"""Pure-Python (Qt-free) helpers for browser-header ("cURL") sign-in.

Google's OAuth *device* flow is currently broken on the server side for many
users (ytmusicapi issue #813), so the app's primary, reliable way to sign in is
to reuse the session that already exists in the user's browser.

The user copies a request to ``music.youtube.com`` as **cURL (bash)** from their
browser's DevTools; this module parses that string, extracts the headers
ytmusicapi needs (``cookie``, ``x-goog-authuser`` …) and writes a JSON file that
``ytmusicapi``'s ``YTMusic(auth=path)`` can load directly (browser auth). The
output format mirrors ytmusicapi's own ``setup_browser`` so the file is
byte-compatible with what the library expects.

This module is deliberately free of any GUI/Qt imports so it can be unit-tested
headlessly. ytmusicapi is imported lazily (with a safe fallback) so importing
this module never touches the network.
"""

from __future__ import annotations

import json
import re
import shlex
import time
from hashlib import sha1
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

# The cookie value ytmusicapi needs to compute the SAPISIDHASH authorization
# header on every request. Without it, browser auth cannot work.
REQUIRED_COOKIE_TOKEN = "__Secure-3PAPISID"

# Headers ytmusicapi drops when building a browser-auth file (see setup_browser).
_IGNORE_HEADERS = {"host", "content-length", "accept-encoding"}

# curl flags that take a value we don't care about (skip the value too).
_VALUE_FLAGS_TO_SKIP = {
    "-d", "--data", "--data-raw", "--data-binary", "--data-ascii",
    "--data-urlencode", "-X", "--request", "-e", "--referer",
    "-A", "--user-agent", "-x", "--proxy", "--url",
}

# Data flags that mark the start of a request body. Chrome may append a binary
# gzip payload after these; parse_curl() truncates at the first occurrence.
_DATA_FLAG_RE = re.compile(r"(?<!\S)(?:--data-raw|--data-binary|--data-ascii|--data-urlencode|--data|-d)(?=\s|=|$)")


class CurlParseError(ValueError):
    """Raised when a pasted cURL string is missing or does not look valid.

    The message is always beginner-friendly and safe to show directly in the UI.
    """


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def parse_curl(curl_text: str) -> dict[str, Any]:
    """Parse a "Copy as cURL (bash)" string into ``{"url": str, "headers": dict}``.

    Header names are lower-cased. Cookies passed via ``-b``/``--cookie`` are
    folded into a ``cookie`` header. Raises :class:`CurlParseError` if the paste
    is empty or clearly not a cURL command.
    """
    if not curl_text or not curl_text.strip():
        raise CurlParseError(
            "Nothing was pasted. Copy the request as cURL (bash) from your browser and paste it here."
        )

    text = curl_text.strip()
    # Normalise line continuations from bash (\ + newline) and Windows carets so
    # the whole command tokenises as one logical line.
    text = text.replace("\\\r\n", " ").replace("\\\n", " ").replace("^\r\n", " ").replace("^\n", " ")

    # Chrome's "Copy as cURL" appends --data-raw $'...' for binary/gzip bodies.
    # This binary blob often contains non-printable characters that corrupt
    # string parsing or shlex. Since we only need the headers and URL (which
    # always appear BEFORE the data), we truncate the string at the data flag.
    match = _DATA_FLAG_RE.search(text)
    if match:
        text = text[:match.start()].strip()

    # Strip bash's ANSI-C quoting prefix ($') if it was left at the end.
    if text.endswith("$'"):
        text = text[:-2].strip()

    # Final safety: remove any non-printable/binary artifacts that might have
    # snuck into the headers part of the string.
    text = "".join(c for c in text if c.isprintable() or c.isspace())

    if "curl" not in text.lower():
        raise CurlParseError(
            "That doesn't look like a cURL command. In DevTools → Network, right-click a "
            "request → Copy → “Copy as cURL (bash)”, then paste it here."
        )

    try:
        tokens = shlex.split(text, posix=True)
    except ValueError:
        # Fall back to a whitespace split if quoting is malformed; the loop below
        # is tolerant of stray tokens.
        tokens = text.split()

    headers: dict[str, str] = {}
    url = ""
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok in ("-H", "--header") and i + 1 < n:
            _add_header(headers, tokens[i + 1])
            i += 2
            continue
        if tok.startswith("-H") and len(tok) > 2:  # glued form: -H'name: value'
            _add_header(headers, tok[2:])
            i += 1
            continue
        if tok in ("-b", "--cookie") and i + 1 < n:
            headers["cookie"] = tokens[i + 1].strip()
            i += 2
            continue
        if tok in _VALUE_FLAGS_TO_SKIP and i + 1 < n:
            i += 2
            continue
        if tok.startswith("http://") or tok.startswith("https://"):
            if not url:
                url = tok
            i += 1
            continue
        i += 1

    return {"url": url, "headers": headers}


def _add_header(headers: dict[str, str], raw: str) -> None:
    """Split a raw ``Name: value`` header string and store it lower-cased."""
    if ":" not in raw:
        return
    name, _, value = raw.partition(":")
    name = name.strip().lower()
    if not name:
        return
    headers[name] = value.strip()


# ---------------------------------------------------------------------------
# Building & saving the ytmusicapi-compatible headers file
# ---------------------------------------------------------------------------
def _initialize_headers() -> dict[str, str]:
    """Return ytmusicapi's default headers (lazy import, with a safe fallback)."""
    try:  # pragma: no cover - exercised implicitly when ytmusicapi is present.
        from ytmusicapi.helpers import initialize_headers

        return dict(initialize_headers())
    except Exception:  # pragma: no cover - keep the module importable without ytmusicapi.
        return {
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
            "accept": "*/*",
            "accept-encoding": "gzip, deflate",
            "content-type": "application/json",
            "content-encoding": "gzip",
            "origin": "https://music.youtube.com",
        }


def build_headers(user_headers: dict[str, str]) -> dict[str, str]:
    """Turn parsed request headers into a ytmusicapi browser-auth header dict.

    Mirrors ytmusicapi's ``setup_browser``: drop ``sec-*`` and a few transport
    headers, then merge in ytmusicapi's default headers.
    """
    cleaned = {
        key: value
        for key, value in user_headers.items()
        if not key.startswith("sec") and key not in _IGNORE_HEADERS
    }
    cleaned.update(_initialize_headers())
    cleaned.setdefault("x-goog-authuser", "0")
    if "cookie" in cleaned and "SAPISIDHASH" not in cleaned.get("authorization", ""):
        origin = cleaned.get("origin") or cleaned.get("x-origin") or "https://music.youtube.com"
        cleaned["authorization"] = _authorization_from_cookie(cleaned["cookie"], origin)
    return cleaned


def _authorization_from_cookie(raw_cookie: str, origin: str) -> str:
    cookie = SimpleCookie()
    cookie.load(raw_cookie.replace('"', ""))
    sapisid = cookie[REQUIRED_COOKIE_TOKEN].value
    timestamp = str(int(time.time()))
    digest = sha1(f"{timestamp} {sapisid} {origin}".encode("utf-8")).hexdigest()
    return f"SAPISIDHASH {timestamp}_{digest}"


def validate_headers(headers: dict[str, str], url: str = "") -> None:
    """Validate parsed headers, raising :class:`CurlParseError` with friendly text."""
    if url and "youtube.com" not in url.lower():
        raise CurlParseError(
            "That request isn't for YouTube Music. Copy a request to "
            "music.youtube.com (not another site) and try again."
        )

    cookie = headers.get("cookie", "")
    if not cookie:
        raise CurlParseError(
            "Couldn't find your sign-in cookie in that paste. Make sure you copied a "
            "request to music.youtube.com as cURL (bash) while signed in."
        )
    if REQUIRED_COOKIE_TOKEN not in cookie:
        raise CurlParseError(
            "This cookie is missing the value the app needs (__Secure-3PAPISID). "
            "Refresh music.youtube.com while signed in, then copy a fresh request and try again."
        )



def save_browser_headers(curl_text: str, dest_path: str) -> dict[str, str]:
    """Parse a pasted cURL string and write a ytmusicapi-ready headers JSON file.

    Returns the final headers dict on success. Raises :class:`CurlParseError`
    (friendly message) if the paste is missing required pieces.
    """
    parsed = parse_curl(curl_text)
    headers = parsed["headers"]
    validate_headers(headers, parsed.get("url", ""))

    # x-goog-authuser is required by ytmusicapi; default to the first account if
    # the browser didn't include it.
    headers.setdefault("x-goog-authuser", "0")

    final = build_headers(headers)
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(final, ensure_ascii=True, indent=4, sort_keys=True),
        encoding="utf-8",
    )
    return final


# ---------------------------------------------------------------------------
# Status of a saved headers file (for the UI's "already signed in" check)
# ---------------------------------------------------------------------------
def browser_file_status(path: Any) -> tuple[str, str]:
    """Classify a browser-header file as ``ok`` / ``missing`` / ``invalid``.

    Returns a ``(status, message)`` tuple with a short, human-friendly message
    suitable for the UI, matching the shape of ``oauth_flow.account_file_status``.
    """
    p = Path(path)
    if not p.is_file():
        return ("missing", "Not signed in yet.")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return ("invalid", "The sign-in file could not be read — please sign in again.")
    if not isinstance(data, dict):
        return ("invalid", "The sign-in file is not in the expected format — please sign in again.")
    lowered = {str(k).lower(): v for k, v in data.items()}
    cookie = str(lowered.get("cookie", ""))
    authorization = str(lowered.get("authorization", ""))
    if not cookie or REQUIRED_COOKIE_TOKEN not in cookie or "SAPISIDHASH" not in authorization:
        return ("invalid", "The saved browser session is incomplete — please sign in again.")
    return ("ok", "Signed in from your browser — ready to use.")
