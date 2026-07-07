#!/usr/bin/env python3
"""Pure-Python (Qt-free) helpers for the in-app YouTube Music OAuth device flow.

This module is deliberately free of any GUI/Qt imports so it can be unit-tested
headlessly and driven from a background worker thread. It provides:

* ``account_file_status(path)`` — quickly classify an OAuth token file as
  ``ok`` / ``missing`` / ``invalid`` with a friendly message.
* ``OAuthSignInEngine`` — a small state machine around ytmusicapi's OAuth
  *device* flow (``get_code`` + poll ``token_from_code``). It is injectable:
  pass a fake ``oauth_factory`` to test the pending/success/denied/expired paths
  without touching the network. On success it finalizes and stores the token
  file so the rest of the app can load it with ``build_client``.

The real credential/token classes are imported lazily so importing this module
never touches the network and so tests can run without ytmusicapi wired to Qt.
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

# The keys ytmusicapi writes into a stored OAuth token file. We only require the
# essential ones so a slightly different ytmusicapi version does not trip us up.
EXPECTED_OAUTH_KEYS = ("access_token", "refresh_token")

# The OAuth scope/verification defaults used by the device flow.
GOOGLE_CLOUD_CONSOLE_URL = "https://console.cloud.google.com/apis/credentials"


def account_file_status(path: Any) -> tuple[str, str]:
    """Classify an OAuth token file.

    Returns a ``(status, message)`` tuple where ``status`` is one of
    ``"ok"``, ``"missing"`` or ``"invalid"`` and ``message`` is a short,
    human-friendly explanation suitable for showing in the UI.
    """
    p = Path(path)
    if not p.is_file():
        return ("missing", "Not signed in yet.")
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        return ("invalid", "The sign-in file could not be read — please sign in again.")
    try:
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return ("invalid", "The sign-in file is not valid — please sign in again.")
    if not isinstance(data, dict):
        return ("invalid", "The sign-in file is not in the expected format — please sign in again.")
    missing = [key for key in EXPECTED_OAUTH_KEYS if not data.get(key)]
    if missing:
        return ("invalid", "The sign-in file is incomplete — please sign in again.")
    return ("ok", "Already signed in — you can reuse this.")


class PollState(Enum):
    """Result states for a single poll of the OAuth device flow."""

    PENDING = "pending"        # user has not authorized yet — keep polling
    SLOW_DOWN = "slow_down"    # Google asked us to poll less often
    SUCCESS = "success"        # authorized; token stored to the account file
    DENIED = "denied"          # user declined the request
    EXPIRED = "expired"        # the device code expired; restart the flow
    ERROR = "error"            # some other, unexpected error


class PollResult:
    """Small value object returned by :meth:`OAuthSignInEngine.poll_once`."""

    def __init__(self, state: PollState, message: str = "", token_path: Optional[str] = None) -> None:
        self.state = state
        self.message = message
        self.token_path = token_path

    @property
    def is_final(self) -> bool:
        return self.state in (PollState.SUCCESS, PollState.DENIED, PollState.EXPIRED, PollState.ERROR)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"PollResult(state={self.state.value!r}, message={self.message!r})"


_PENDING_MESSAGES = {
    PollState.PENDING: "Waiting for you to approve in the browser…",
    PollState.SLOW_DOWN: "Waiting for you to approve in the browser…",
}
_FINAL_MESSAGES = {
    PollState.SUCCESS: "Signed in successfully.",
    PollState.DENIED: "Sign-in was declined. Click “Try again” to restart.",
    PollState.EXPIRED: "The sign-in request expired. Click “Try again” to get a fresh code.",
    PollState.ERROR: "Sign-in could not be completed. Click “Try again”.",
}


def _default_oauth_factory(client_id: str, client_secret: str) -> Any:
    """Create a real ``OAuthCredentials`` object (imported lazily)."""
    from ytmusicapi import OAuthCredentials

    return OAuthCredentials(client_id, client_secret)


def _default_store_token(oauth: Any, raw: dict[str, Any], token_path: str) -> None:
    """Finalize a raw token dict into a stored OAuth token file (lazy import).

    This mirrors ytmusicapi's own :meth:`RefreshingToken.prompt_for_token`
    exactly (as corrected in ytmusicapi 1.12.1, PR #927). Google's device-flow
    token response contains a ``refresh_token_expires_in`` field that the strict
    ``Token`` dataclass does not accept, so we must not blindly splat ``raw`` into
    the constructor. Instead we pass only the fields the token models and:

    * set ``expires_in`` to the *refresh token* lifetime
      (``refresh_token_expires_in``), falling back to the access-token lifetime,
      and
    * call ``update(raw)`` so ``expires_at`` is computed from the *access token*
      ``expires_in`` as an absolute UNIX epoch (avoiding immediate re-expiry).

    Assigning ``local_cache`` writes the file via the library's own store path
    and keeps it in sync when the access token is auto-refreshed later.
    """
    from ytmusicapi.auth.oauth.token import RefreshingToken

    refresh_token_expires_in = raw.get("refresh_token_expires_in", raw["expires_in"])
    ref = RefreshingToken(
        credentials=oauth,
        access_token=raw["access_token"],
        refresh_token=raw["refresh_token"],
        scope=raw["scope"],
        token_type=raw["token_type"],
        expires_in=refresh_token_expires_in,
    )
    ref.update(raw)
    Path(token_path).parent.mkdir(parents=True, exist_ok=True)
    ref.local_cache = Path(token_path)


class OAuthSignInEngine:
    """Drive ytmusicapi's OAuth *device* flow from a background thread.

    Usage::

        engine = OAuthSignInEngine(client_id, client_secret, token_path)
        code = engine.begin()                 # -> dict with user_code, etc.
        # show engine.verification_url + code["user_code"] to the user
        while True:
            result = engine.poll_once()
            if result.is_final:
                break
            time.sleep(engine.interval)

    Both ``oauth_factory`` and ``store_token`` are injectable so tests can drive
    the pending → success / denied / expired paths with a fake OAuth object and
    without writing real tokens.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_path: Any,
        oauth_factory: Optional[Callable[[str, str], Any]] = None,
        store_token: Optional[Callable[[Any, dict[str, Any], str], None]] = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_path = str(token_path)
        self._oauth_factory = oauth_factory or _default_oauth_factory
        self._store_token = store_token or _default_store_token
        self._oauth: Any = None
        self._code: dict[str, Any] = {}
        self.interval: float = 5.0
        self.verification_url: str = ""
        self.user_code: str = ""

    def begin(self) -> dict[str, Any]:
        """Request a device code. Returns the raw code dict from ytmusicapi."""
        self._oauth = self._oauth_factory(self.client_id, self.client_secret)
        code = self._oauth.get_code()
        if not isinstance(code, dict) or "device_code" not in code:
            raise ValueError("Unexpected response while starting sign-in.")
        self._code = code
        try:
            self.interval = float(code.get("interval", 5) or 5)
        except (TypeError, ValueError):
            self.interval = 5.0
        self.user_code = str(code.get("user_code", ""))
        base_url = str(code.get("verification_url", "https://www.google.com/device"))
        if self.user_code:
            self.verification_url = f"{base_url}?user_code={self.user_code}"
        else:
            self.verification_url = base_url
        return code

    def bump_interval(self, seconds: float = 5.0) -> None:
        """Increase the polling interval (used when Google says ``slow_down``)."""
        self.interval = float(self.interval) + float(seconds)

    def poll_once(self) -> PollResult:
        """Poll the token endpoint exactly once and map the outcome to a state."""
        if self._oauth is None or not self._code:
            return PollResult(PollState.ERROR, "Sign-in has not been started yet.")
        try:
            raw = self._oauth.token_from_code(self._code["device_code"])
        except Exception as exc:  # noqa: BLE001 - map any transport error to a state.
            return PollResult(PollState.ERROR, _error_message_from_exc(exc))

        if isinstance(raw, dict) and "error" in raw:
            error = str(raw.get("error", "")).lower()
            if error == "authorization_pending":
                return PollResult(PollState.PENDING, _PENDING_MESSAGES[PollState.PENDING])
            if error == "slow_down":
                self.bump_interval(5.0)
                return PollResult(PollState.SLOW_DOWN, _PENDING_MESSAGES[PollState.SLOW_DOWN])
            if error == "access_denied":
                return PollResult(PollState.DENIED, _FINAL_MESSAGES[PollState.DENIED])
            if error in ("expired_token", "token_expired"):
                return PollResult(PollState.EXPIRED, _FINAL_MESSAGES[PollState.EXPIRED])
            return PollResult(PollState.ERROR, _FINAL_MESSAGES[PollState.ERROR])

        if not isinstance(raw, dict):
            return PollResult(PollState.ERROR, _FINAL_MESSAGES[PollState.ERROR])

        # Success — finalize and persist the token file.
        try:
            self._store_token(self._oauth, raw, self.token_path)
        except Exception as exc:  # noqa: BLE001 - storing failed; surface friendly.
            return PollResult(PollState.ERROR, f"Signed in, but saving the token failed: {exc}")
        return PollResult(PollState.SUCCESS, _FINAL_MESSAGES[PollState.SUCCESS], token_path=self.token_path)


def _error_message_from_exc(exc: BaseException) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    lowered = text.lower()
    if "denied" in lowered:
        return _FINAL_MESSAGES[PollState.DENIED]
    if "expired" in lowered:
        return _FINAL_MESSAGES[PollState.EXPIRED]
    return "Could not reach Google to check sign-in status. Check your connection and try again."
