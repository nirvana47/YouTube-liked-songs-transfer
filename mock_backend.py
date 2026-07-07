#!/usr/bin/env python3
"""Qt-free mock YouTube Music backend and fault-injection presets.

This lets every success/error/exit scenario be exercised WITHOUT real
credentials or network access — both from the automated tests and from the
in-app *demo mode*.

Contents:
* Fake exception types that look like the real ones (HTTP 429, timeout, auth).
* ``MockYTMusic`` — a stand-in for ``ytmusicapi.YTMusic`` implementing
  ``get_liked_songs``, ``rate_song``, ``create_playlist`` and
  ``add_playlist_items`` with scriptable faults.
* ``SCENARIOS`` — named presets covering the happy path and every failure mode.
* ``FakeOAuth`` — an object compatible with ``oauth_flow.OAuthSignInEngine`` for
  testing the pending → success / denied / expired sign-in paths offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Fake exceptions that mimic the shape real errors have (so friendly_error and
# classify_retryable treat them correctly).
# ---------------------------------------------------------------------------
class MockRateLimitError(Exception):
    """Looks like an HTTP 429 response."""

    def __init__(self, message: str = "429 Too Many Requests") -> None:
        super().__init__(message)
        self.status_code = 429


class MockNetworkTimeout(TimeoutError):
    """Looks like a requests/stdlib network timeout."""


class MockAuthError(Exception):
    """Looks like an HTTP 401/403 authentication failure."""

    def __init__(self, message: str = "401 Unauthorized", status_code: int = 401) -> None:
        super().__init__(message)
        self.status_code = status_code


class MockTrackError(Exception):
    """A per-track failure that is NOT retryable (partial-transfer path)."""


class MockPlaylistError(Exception):
    """A playlist-creation failure that is NOT retryable."""


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------
def _sample_track(index: int, available: bool = True) -> dict[str, Any]:
    return {
        "videoId": f"vid{index:03d}",
        "title": f"Sample Song {index}",
        "artists": [{"name": f"Artist {index}"}],
        "album": {"name": f"Album {index}"},
        "isAvailable": available,
    }


def sample_payload(count: int = 6, unavailable_indices: Optional[set[int]] = None) -> dict[str, Any]:
    """Build a get_liked_songs-shaped payload with ``count`` tracks."""
    unavailable_indices = unavailable_indices or set()
    tracks = [_sample_track(i, available=i not in unavailable_indices) for i in range(1, count + 1)]
    return {"tracks": tracks}


# ---------------------------------------------------------------------------
# Fault-injection configuration
# ---------------------------------------------------------------------------
@dataclass
class MockConfig:
    """Scripts how the mock behaves. All faults default to off."""

    payload: dict[str, Any] = field(default_factory=lambda: sample_payload(6))
    target_liked_ids: set[str] = field(default_factory=set)

    # get_liked_songs faults
    liked_raise: Optional[BaseException] = None            # raise this on every get_liked_songs

    # rate_song faults
    rate_limit_start: Optional[int] = None                 # 1-based rate_song call to start 429s
    rate_limit_burst: int = 0                              # how many consecutive 429s to raise
    fail_track_indices: set[int] = field(default_factory=set)  # 1-based like-call indices that fail
    rate_network_start: Optional[int] = None               # rate_song call to start timeouts
    rate_network_burst: int = 0

    # playlist faults
    playlist_create_raise: Optional[BaseException] = None
    playlist_add_raise: Optional[BaseException] = None


class MockYTMusic:
    """A minimal, scriptable stand-in for ``ytmusicapi.YTMusic``."""

    def __init__(self, config: Optional[MockConfig] = None) -> None:
        self.config = config or MockConfig()
        self.get_liked_calls = 0
        self.rate_calls = 0
        self.create_playlist_calls = 0
        self.add_items_calls = 0
        self.liked_video_ids: list[str] = []
        self._rl_remaining = int(self.config.rate_limit_burst)
        self._net_remaining = int(self.config.rate_network_burst)
        self._playlist_counter = 0

    # -- reads ---------------------------------------------------------------
    def get_liked_songs(self, limit: Optional[int] = None) -> dict[str, Any]:
        self.get_liked_calls += 1
        if self.config.liked_raise is not None:
            raise self.config.liked_raise
        payload = self.config.payload
        if limit is not None and isinstance(payload, dict):
            tracks = list(payload.get("tracks") or [])
            return {"tracks": tracks[:limit]}
        return payload

    # -- writes --------------------------------------------------------------
    def rate_song(self, video_id: str, status: Any) -> dict[str, Any]:
        self.rate_calls += 1
        call = self.rate_calls
        cfg = self.config

        # Injected 429 burst (retryable — should recover).
        if cfg.rate_limit_start is not None and call >= cfg.rate_limit_start and self._rl_remaining > 0:
            self._rl_remaining -= 1
            raise MockRateLimitError()

        # Injected transient network burst (retryable).
        if cfg.rate_network_start is not None and call >= cfg.rate_network_start and self._net_remaining > 0:
            self._net_remaining -= 1
            raise MockNetworkTimeout("Connection timed out")

        # Injected per-track hard failure (NOT retryable — recorded + continue).
        if call in cfg.fail_track_indices:
            raise MockTrackError(f"Could not rate {video_id}")

        self.liked_video_ids.append(video_id)
        return {"status": "ok"}

    def create_playlist(
        self,
        title: str,
        description: str = "",
        privacy_status: str = "PRIVATE",
        video_ids: Optional[list[str]] = None,
    ) -> str:
        self.create_playlist_calls += 1
        if self.config.playlist_create_raise is not None:
            raise self.config.playlist_create_raise
        self._playlist_counter += 1
        return f"PL_MOCK_{self._playlist_counter:04d}"

    def add_playlist_items(self, playlist_id: str, videoIds: list[str], duplicates: bool = False) -> dict[str, Any]:  # noqa: N803 - match ytmusicapi signature.
        self.add_items_calls += 1
        if self.config.playlist_add_raise is not None:
            raise self.config.playlist_add_raise
        return {"status": "ok", "added": len(videoIds)}


# ---------------------------------------------------------------------------
# Named scenario presets
# ---------------------------------------------------------------------------
def _scenario_config(name: str) -> MockConfig:
    if name == "happy":
        return MockConfig(payload=sample_payload(6))
    if name == "empty_liked":
        return MockConfig(payload={"tracks": []})
    if name == "some_unavailable":
        return MockConfig(payload=sample_payload(6, unavailable_indices={2, 4}))
    if name == "rate_limit_midway":
        # The 2nd rate_song call (track 2) gets two 429s, then recovers.
        return MockConfig(payload=sample_payload(5), rate_limit_start=2, rate_limit_burst=2)
    if name == "network_timeout":
        # get_liked_songs times out persistently -> friendly message + retries.
        return MockConfig(payload=sample_payload(4), liked_raise=MockNetworkTimeout("Connection timed out"))
    if name == "partial_failures":
        # Tracks 2 and 4 fail permanently; the rest succeed.
        return MockConfig(payload=sample_payload(5), fail_track_indices={2, 4})
    if name == "playlist_fail":
        return MockConfig(payload=sample_payload(4), playlist_create_raise=MockPlaylistError("Playlist quota reached"))
    if name == "bad_auth":
        return MockConfig(payload=sample_payload(4), liked_raise=MockAuthError("401 Unauthorized"))
    if name == "expired_token":
        return MockConfig(payload=sample_payload(4), liked_raise=MockAuthError("invalid_grant: expired token", status_code=401))
    raise KeyError(f"Unknown scenario: {name!r}")


SCENARIOS = {
    "happy",
    "empty_liked",
    "some_unavailable",
    "rate_limit_midway",
    "network_timeout",
    "partial_failures",
    "playlist_fail",
    "bad_auth",
    "expired_token",
}

# Human-friendly one-liners for the demo-mode scenario picker.
SCENARIO_LABELS = {
    "happy": "Happy path — everything succeeds",
    "empty_liked": "No liked songs to transfer",
    "some_unavailable": "Some songs are unavailable",
    "rate_limit_midway": "Rate limited mid-transfer (auto-retries)",
    "network_timeout": "Network timeout (friendly error + retry)",
    "partial_failures": "Some songs fail (partial success)",
    "playlist_fail": "Playlist creation fails (partial success)",
    "bad_auth": "Authentication refused",
    "expired_token": "Sign-in token expired",
}


def make_client(scenario: str) -> MockYTMusic:
    """Build a fresh ``MockYTMusic`` configured for a named scenario."""
    return MockYTMusic(_scenario_config(scenario))


def make_config(scenario: str) -> MockConfig:
    return _scenario_config(scenario)


# ---------------------------------------------------------------------------
# Fake OAuth object compatible with oauth_flow.OAuthSignInEngine
# ---------------------------------------------------------------------------
class FakeOAuth:
    """A fake OAuthCredentials-like object for offline sign-in testing.

    ``mode`` selects the behavior of the poll loop:
      * ``"success"`` — returns ``pending`` ``pending_times`` times, then a
        valid token dict.
      * ``"denied"`` — returns an ``access_denied`` error.
      * ``"expired"`` — returns an ``expired_token`` error.
      * ``"slow_down"`` — returns ``slow_down`` once, then behaves like success.
    """

    def __init__(self, mode: str = "success", pending_times: int = 2) -> None:
        self.mode = mode
        self.pending_times = pending_times
        self._polls = 0
        self._slowed = False

    def get_code(self) -> dict[str, Any]:
        return {
            "device_code": "FAKE-DEVICE-CODE",
            "user_code": "ABCD-EFGH",
            "expires_in": 1800,
            "interval": 0,  # 0 so tests do not sleep
            "verification_url": "https://www.google.com/device",
        }

    def token_from_code(self, device_code: str) -> dict[str, Any]:
        self._polls += 1
        if self.mode == "denied":
            return {"error": "access_denied"}
        if self.mode == "expired":
            return {"error": "expired_token"}
        if self.mode == "slow_down" and not self._slowed:
            self._slowed = True
            return {"error": "slow_down"}
        if self._polls <= self.pending_times:
            return {"error": "authorization_pending"}
        return {
            "scope": "https://www.googleapis.com/auth/youtube",
            "token_type": "Bearer",
            "access_token": "FAKE-ACCESS-TOKEN",
            "refresh_token": "FAKE-REFRESH-TOKEN",
            "expires_in": 3600,
        }


def fake_oauth_factory(mode: str = "success", pending_times: int = 2):
    """Return an ``oauth_factory`` callable that ignores creds and yields FakeOAuth."""

    def factory(client_id: str, client_secret: str) -> FakeOAuth:
        return FakeOAuth(mode=mode, pending_times=pending_times)

    return factory


def store_token_json(oauth: Any, raw: dict[str, Any], token_path: str) -> None:
    """A ``store_token`` implementation that just writes the raw dict as JSON.

    Used by tests/demo so finalizing a fake sign-in does not require the real
    RefreshingToken machinery.
    """
    import json
    from pathlib import Path

    p = Path(token_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(raw, indent=2), encoding="utf-8")
