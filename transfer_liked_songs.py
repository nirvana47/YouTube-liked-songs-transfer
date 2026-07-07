#!/usr/bin/env python3
"""Transfer YouTube Music liked songs from one account to another.

This script uses ytmusicapi authentication files:
- source account: reads the special "Liked Songs" playlist
- target account: thumbs-up the same video IDs, and optionally creates a playlist

OAuth tokens are stored through keyring/macOS Keychain. Browser-header JSON files remain supported.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import tempfile
import time
from contextlib import AbstractContextManager, nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar

import keyring
from ytmusicapi import OAuthCredentials, YTMusic
from ytmusicapi.models.content.enums import LikeStatus

try:
    from rich.console import Console
    from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeElapsedColumn
except ImportError:  # pragma: no cover - fallback for minimal environments
    Console = None  # type: ignore[assignment]
    Progress = None  # type: ignore[assignment]
    BarColumn = SpinnerColumn = TaskProgressColumn = TextColumn = TimeElapsedColumn = None  # type: ignore[assignment]


DEFAULT_SOURCE_AUTH = "auth/account_a.json"
DEFAULT_TARGET_AUTH = "auth/account_b.json"
DEFAULT_REPORT = "transfer_report.json"
DEFAULT_ENV_FILE = ".env"
DEFAULT_KEYRING_SERVICE = "YouTube-liked-songs-transfer"
DEFAULT_RATE_LIMIT_RETRIES = 5
DEFAULT_RATE_LIMIT_INITIAL_BACKOFF = 2.0

T = TypeVar("T")
console = Console() if Console else None


def say(message: str, *, error: bool = False) -> None:
    if console:
        output_console = Console(stderr=error) if error else console
        output_console.print(message)
    else:
        print(message, file=sys.stderr if error else sys.stdout)


def human_error(exc: BaseException) -> str:
    message = str(exc).strip() or repr(exc)
    message = " ".join(message.split())
    if "429" in message:
        return "YouTube Music is rate limiting requests. Try again later, or rerun with --sleep 1.0."
    if "Missing auth file" in message:
        return f"{message}. Create the auth files first, or use --auth-mode oauth with OAuth token files."
    if "Please provide authentication" in message or "authentication" in message.lower():
        return "Authentication failed. Recreate the auth file for the correct Google account, then run --dry-run again."
    if "client id" in message.lower() or "client secret" in message.lower():
        return "OAuth client ID/secret are missing. Copy .env.example to .env and fill in your Google OAuth values."
    return message


@dataclass(frozen=True)
class Track:
    video_id: str
    title: str | None = None
    artists: str | None = None
    album: str | None = None
    is_available: bool | None = None


@dataclass
class TransferReport:
    started_at: str
    source_auth: str
    target_auth: str
    limit: int | None
    dry_run: bool
    total_source_tracks: int = 0
    unique_video_ids: int = 0
    already_liked_on_target: int = 0
    liked_on_target: int = 0
    playlist_id: str | None = None
    playlist_added: int = 0
    skipped: list[dict[str, Any]] | None = None
    failed: list[dict[str, Any]] | None = None
    finished_at: str | None = None


def parse_args() -> argparse.Namespace:
    load_env_file(DEFAULT_ENV_FILE)
    parser = argparse.ArgumentParser(
        prog="transfer_liked_songs.py",
        description=(
            "Copy liked songs from YouTube Music account A to account B. "
            "Start with --dry-run, then rerun with --confirm when the preview looks right."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  Preview the transfer safely:
    python3 transfer_liked_songs.py --dry-run

  Recommended OAuth dry run on macOS:
    python3 transfer_liked_songs.py --auth-mode oauth \\
      --source-auth auth/account_a_oauth.json \\
      --target-auth auth/account_b_oauth.json \\
      --skip-existing-target-likes --dry-run

  Run the real transfer after checking the preview:
    python3 transfer_liked_songs.py --auth-mode oauth \\
      --source-auth auth/account_a_oauth.json \\
      --target-auth auth/account_b_oauth.json \\
      --skip-existing-target-likes --confirm

  Also create a private playlist on account B:
    python3 transfer_liked_songs.py --confirm \\
      --playlist-title "Imported liked songs"
""",
    )
    parser.add_argument(
        "--source-auth",
        default=DEFAULT_SOURCE_AUTH,
        metavar="PATH",
        help=f"Account A auth file to read liked songs from. Default: {DEFAULT_SOURCE_AUTH}",
    )
    parser.add_argument(
        "--target-auth",
        default=DEFAULT_TARGET_AUTH,
        metavar="PATH",
        help=f"Account B auth file to write likes/playlists to. Default: {DEFAULT_TARGET_AUTH}",
    )
    parser.add_argument(
        "--auth-mode",
        choices=("browser", "oauth"),
        default="browser",
        help="Authentication file type. Use oauth to avoid browser-header copy/paste. Default: browser.",
    )
    parser.add_argument(
        "--oauth-client-id",
        default=os.getenv("YTMUSICAPI_CLIENT_ID"),
        help="OAuth client ID for --auth-mode oauth. Defaults to YTMUSICAPI_CLIENT_ID.",
    )
    parser.add_argument(
        "--oauth-client-secret",
        default=os.getenv("YTMUSICAPI_CLIENT_SECRET"),
        help="OAuth client secret for --auth-mode oauth. Defaults to YTMUSICAPI_CLIENT_SECRET.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum liked songs to read from account A. Omit for all available liked songs.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read and report what would be transferred without changing account B.",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required for non-dry-run writes to account B. Without it, an interactive prompt is shown when possible.",
    )
    parser.add_argument(
        "--skip-existing-target-likes",
        action="store_true",
        help="Fetch account B liked songs first and skip songs that are already liked there.",
    )
    parser.add_argument(
        "--skip-source-unavailable",
        action="store_true",
        help="Skip source tracks that YouTube Music marks unavailable in account A's liked songs response.",
    )
    parser.add_argument(
        "--no-like",
        action="store_true",
        help="Do not thumbs-up songs on account B. Useful with --playlist-title only.",
    )
    parser.add_argument(
        "--playlist-title",
        help="Optional: create a private playlist on account B containing the transferred songs.",
    )
    parser.add_argument(
        "--playlist-description",
        default="Created by transfer_liked_songs.py from account A liked songs.",
        help="Description for --playlist-title.",
    )
    parser.add_argument(
        "--playlist-privacy",
        choices=("PRIVATE", "UNLISTED", "PUBLIC"),
        default="PRIVATE",
        help="Privacy for --playlist-title. Default: PRIVATE.",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=50,
        help="Number of video IDs per playlist insertion request. Default: 50.",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.25,
        help="Seconds to sleep between target-account write calls. Default: 0.25.",
    )
    parser.add_argument(
        "--report",
        default=DEFAULT_REPORT,
        help=f"Where to write a JSON transfer report. Default: {DEFAULT_REPORT}",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop at the first target-account write failure instead of recording and continuing.",
    )
    parser.add_argument(
        "--keyring-service",
        default=os.getenv("YTLS_KEYRING_SERVICE", DEFAULT_KEYRING_SERVICE),
        help=f"Keychain/keyring service name for OAuth token storage. Default: {DEFAULT_KEYRING_SERVICE}",
    )
    parser.add_argument(
        "--source-keyring-account",
        default=os.getenv("YTLS_SOURCE_KEYRING_ACCOUNT", "source"),
        help="Keychain/keyring account name for account A's OAuth token. Default: source",
    )
    parser.add_argument(
        "--target-keyring-account",
        default=os.getenv("YTLS_TARGET_KEYRING_ACCOUNT", "target"),
        help="Keychain/keyring account name for account B's OAuth token. Default: target",
    )
    parser.add_argument(
        "--rate-limit-retries",
        type=int,
        default=DEFAULT_RATE_LIMIT_RETRIES,
        help=f"Maximum retries for HTTP 429 responses. Default: {DEFAULT_RATE_LIMIT_RETRIES}",
    )
    parser.add_argument(
        "--rate-limit-initial-backoff",
        type=float,
        default=DEFAULT_RATE_LIMIT_INITIAL_BACKOFF,
        help=f"Initial exponential backoff seconds for HTTP 429 responses. Default: {DEFAULT_RATE_LIMIT_INITIAL_BACKOFF}",
    )
    return parser.parse_args()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_env_file(path: str) -> None:
    env_path = Path(path)
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def require_file(path: str) -> None:
    if not Path(path).is_file():
        raise SystemExit(f"Missing auth file: {path}")


def load_oauth_token_from_keyring(service: str, account: str) -> str | None:
    try:
        return keyring.get_password(service, account)
    except keyring.errors.KeyringError as exc:
        raise SystemExit(f"Unable to read OAuth token from Keychain/keyring for {account!r}: {exc}") from exc


def save_oauth_token_to_keyring(service: str, account: str, token_json: str) -> None:
    try:
        keyring.set_password(service, account, token_json)
    except keyring.errors.KeyringError as exc:
        raise SystemExit(f"Unable to save OAuth token to Keychain/keyring for {account!r}: {exc}") from exc


class OAuthKeyringAuthFile(AbstractContextManager[str]):
    """Materialize an OAuth token from Keychain/keyring only for ytmusicapi runtime use."""

    def __init__(self, source_file: str, service: str, account: str) -> None:
        self.source_file = Path(source_file)
        self.service = service
        self.account = account
        self._temp_file: tempfile.NamedTemporaryFile[str] | None = None

    def __enter__(self) -> str:
        token_json = self._load_or_import_token()
        self._temp_file = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".json",
            prefix=f"ytmusicapi-{self.account}-",
            delete=False,
        )
        try:
            os.chmod(self._temp_file.name, 0o600)
            self._temp_file.write(token_json)
            self._temp_file.flush()
            return self._temp_file.name
        finally:
            self._temp_file.close()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if not self._temp_file:
            return
        temp_path = Path(self._temp_file.name)
        try:
            if temp_path.is_file():
                token_json = temp_path.read_text(encoding="utf-8")
                json.loads(token_json)
                save_oauth_token_to_keyring(self.service, self.account, token_json)
                temp_path.unlink()
        finally:
            self._temp_file = None

    def _load_or_import_token(self) -> str:
        keyring_token = load_oauth_token_from_keyring(self.service, self.account)
        if keyring_token:
            return keyring_token
        if not self.source_file.is_file():
            raise SystemExit(
                f"Missing OAuth token for {self.account!r}. Create {self.source_file} once with "
                "ytmusicapi oauth, or store a token in Keychain/keyring first."
            )
        token_json = self.source_file.read_text(encoding="utf-8")
        json.loads(token_json)
        save_oauth_token_to_keyring(self.service, self.account, token_json)
        say(f"Imported OAuth token into Keychain/keyring account {self.account!r}.")
        return token_json


def build_client(auth_file: str, auth_mode: str, client_id: str | None, client_secret: str | None) -> YTMusic:
    if auth_mode == "oauth":
        if not client_id or not client_secret:
            raise SystemExit(
                "OAuth mode requires --oauth-client-id/--oauth-client-secret or "
                "YTMUSICAPI_CLIENT_ID/YTMUSICAPI_CLIENT_SECRET."
            )
        return YTMusic(auth_file, oauth_credentials=OAuthCredentials(client_id, client_secret))
    return YTMusic(auth_file)


def extract_tracks(liked_payload: Any) -> list[Track]:
    """Handle ytmusicapi's playlist-shaped get_liked_songs response."""
    raw_tracks: Iterable[dict[str, Any]]
    if isinstance(liked_payload, dict):
        raw_tracks = liked_payload.get("tracks") or []
    elif isinstance(liked_payload, list):
        raw_tracks = liked_payload
    else:
        raw_tracks = []

    tracks: list[Track] = []
    for item in raw_tracks:
        video_id = item.get("videoId")
        if not video_id:
            continue
        tracks.append(
            Track(
                video_id=video_id,
                title=item.get("title"),
                artists=format_artists(item.get("artists")),
                album=format_album(item.get("album")),
                is_available=item.get("isAvailable"),
            )
        )
    return tracks


def format_artists(value: Any) -> str | None:
    if not isinstance(value, list):
        return None
    names = [artist.get("name") for artist in value if isinstance(artist, dict) and artist.get("name")]
    return ", ".join(names) if names else None


def format_album(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    return value.get("name")


def dedupe_tracks(tracks: Iterable[Track]) -> tuple[list[Track], list[dict[str, Any]]]:
    seen: set[str] = set()
    unique: list[Track] = []
    skipped: list[dict[str, Any]] = []
    for track in tracks:
        if track.video_id in seen:
            skipped.append({"reason": "duplicate_in_source", "track": asdict(track)})
            continue
        seen.add(track.video_id)
        unique.append(track)
    return unique, skipped


def chunked(values: list[str], chunk_size: int) -> Iterable[list[str]]:
    if chunk_size < 1:
        raise ValueError("--chunk-size must be at least 1")
    for start in range(0, len(values), chunk_size):
        yield values[start : start + chunk_size]


def get_liked_video_ids(client: YTMusic, limit: int | None, max_retries: int, initial_backoff: float) -> set[str]:
    tracks = extract_tracks(
        call_with_rate_limit_retry(
            "get_liked_songs",
            lambda: client.get_liked_songs(limit=limit),
            max_retries,
            initial_backoff,
        )
    )
    return {track.video_id for track in tracks}


def write_report(path: str, report: TransferReport) -> None:
    payload = asdict(report)
    report_path = Path(path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def is_rate_limit_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    response_status = getattr(response, "status_code", None)
    return status_code == 429 or response_status == 429 or "429" in repr(exc)


def call_with_rate_limit_retry(
    operation_name: str,
    func: Callable[[], T],
    max_retries: int,
    initial_backoff: float,
) -> T:
    attempts = max(0, max_retries) + 1
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except Exception as exc:
            if not is_rate_limit_error(exc) or attempt >= attempts:
                raise
            delay = max(0.0, initial_backoff) * (2 ** (attempt - 1))
            delay += random.uniform(0, min(1.0, delay * 0.1)) if delay > 0 else 0
            say(
                f"YouTube Music is rate limiting {operation_name}; retrying in {delay:.1f}s "
                f"(attempt {attempt}/{attempts - 1}).",
                error=True,
            )
            time.sleep(delay)
    raise RuntimeError(f"Unreachable retry state for {operation_name}")


def require_write_confirmation(args: argparse.Namespace) -> None:
    if args.dry_run or args.confirm:
        return
    if not sys.stdin.isatty():
        raise SystemExit("Refusing to write to account B without --confirm. Re-run with --confirm or --dry-run.")
    response = input("This will write likes/playlists to account B. Type CONFIRM to continue: ").strip()
    if response != "CONFIRM":
        raise SystemExit("Cancelled before any target-account writes.")


def like_tracks_on_target(
    target: YTMusic,
    tracks: list[Track],
    report: TransferReport,
    sleep_seconds: float,
    fail_fast: bool,
    max_retries: int,
    initial_backoff: float,
) -> None:
    progress_context = (
        Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console,
        )
        if Progress and console
        else nullcontext(None)
    )
    with progress_context as progress:
        task_id = progress.add_task("Liking songs on account B", total=len(tracks)) if progress else None
        for index, track in enumerate(tracks, start=1):
            try:
                call_with_rate_limit_retry(
                    "rate_song",
                    lambda: target.rate_song(track.video_id, LikeStatus.LIKE),
                    max_retries,
                    initial_backoff,
                )
                report.liked_on_target += 1
                if not progress:
                    say(f"[like {index}/{len(tracks)}] {track.video_id} {track.title or ''}".rstrip())
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)
            except Exception as exc:  # ytmusicapi raises several request/user/server exceptions.
                failure = {"action": "like", "track": asdict(track), "error": human_error(exc)}
                report.failed = report.failed or []
                report.failed.append(failure)
                say(f"Could not like {track.title or track.video_id}: {human_error(exc)}", error=True)
                if fail_fast:
                    raise
            finally:
                if progress and task_id is not None:
                    progress.advance(task_id)


def create_playlist_on_target(
    target: YTMusic,
    video_ids: list[str],
    title: str,
    description: str,
    privacy: str,
    chunk_size: int,
    sleep_seconds: float,
    report: TransferReport,
    fail_fast: bool,
    max_retries: int,
    initial_backoff: float,
) -> None:
    if not video_ids:
        return

    chunks = iter(chunked(video_ids, chunk_size))
    first_chunk = next(chunks)
    try:
        playlist_id = call_with_rate_limit_retry(
            "create_playlist",
            lambda: target.create_playlist(
                title=title,
                description=description,
                privacy_status=privacy,
                video_ids=first_chunk,
            ),
            max_retries,
            initial_backoff,
        )
        if not isinstance(playlist_id, str):
            raise RuntimeError(f"Unexpected create_playlist response: {playlist_id!r}")
        report.playlist_id = playlist_id
        report.playlist_added += len(first_chunk)
        say(f"Created playlist {playlist_id} with {len(first_chunk)} tracks.")
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    except Exception as exc:
        failure = {"action": "create_playlist", "title": title, "error": human_error(exc)}
        report.failed = report.failed or []
        report.failed.append(failure)
        say(f"Could not create playlist: {human_error(exc)}", error=True)
        if fail_fast:
            raise
        return

    for index, video_id_chunk in enumerate(chunks, start=2):
        try:
            call_with_rate_limit_retry(
                "add_playlist_items",
                lambda: target.add_playlist_items(report.playlist_id, videoIds=video_id_chunk, duplicates=False),
                max_retries,
                initial_backoff,
            )
            report.playlist_added += len(video_id_chunk)
            say(f"Added playlist batch {index}: {len(video_id_chunk)} tracks.")
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
        except Exception as exc:
            failure = {
                "action": "add_playlist_items",
                "playlist_id": report.playlist_id,
                "video_ids": video_id_chunk,
                "error": human_error(exc),
            }
            report.failed = report.failed or []
            report.failed.append(failure)
            say(f"Could not add playlist batch {index}: {human_error(exc)}", error=True)
            if fail_fast:
                raise


def main() -> int:
    args = parse_args()
    if args.auth_mode == "browser":
        require_file(args.source_auth)
        require_file(args.target_auth)

    report = TransferReport(
        started_at=utc_now(),
        source_auth="keyring" if args.auth_mode == "oauth" else args.source_auth,
        target_auth="keyring" if args.auth_mode == "oauth" else args.target_auth,
        limit=args.limit,
        dry_run=args.dry_run,
        skipped=[],
        failed=[],
    )

    source_auth_context: AbstractContextManager[str]
    target_auth_context: AbstractContextManager[str]
    if args.auth_mode == "oauth":
        source_auth_context = OAuthKeyringAuthFile(
            args.source_auth,
            args.keyring_service,
            args.source_keyring_account,
        )
        target_auth_context = OAuthKeyringAuthFile(
            args.target_auth,
            args.keyring_service,
            args.target_keyring_account,
        )
    else:
        source_auth_context = nullcontext(args.source_auth)
        target_auth_context = nullcontext(args.target_auth)

    with source_auth_context as source_auth, target_auth_context as target_auth:
        source = build_client(source_auth, args.auth_mode, args.oauth_client_id, args.oauth_client_secret)
        target = build_client(target_auth, args.auth_mode, args.oauth_client_id, args.oauth_client_secret)

        say("Reading liked songs from account A...")
        source_tracks = extract_tracks(
            call_with_rate_limit_retry(
                "get_liked_songs",
                lambda: source.get_liked_songs(limit=args.limit),
                args.rate_limit_retries,
                args.rate_limit_initial_backoff,
            )
        )
        report.total_source_tracks = len(source_tracks)

        unique_tracks, skipped = dedupe_tracks(source_tracks)
        report.skipped = skipped
        report.unique_video_ids = len(unique_tracks)

        if args.skip_source_unavailable:
            available_tracks = []
            for track in unique_tracks:
                if track.is_available is False:
                    report.skipped = report.skipped or []
                    report.skipped.append({"reason": "unavailable_in_source", "track": asdict(track)})
                    continue
                available_tracks.append(track)
            unique_tracks = available_tracks

        if args.skip_existing_target_likes:
            say("Reading existing liked songs from account B...")
            target_likes = get_liked_video_ids(
                target,
                limit=None,
                max_retries=args.rate_limit_retries,
                initial_backoff=args.rate_limit_initial_backoff,
            )
            before = len(unique_tracks)
            unique_tracks = [track for track in unique_tracks if track.video_id not in target_likes]
            report.already_liked_on_target = before - len(unique_tracks)

        if not unique_tracks:
            say("No tracks to transfer after filtering.")
            report.finished_at = utc_now()
            write_report(args.report, report)
            return 0

        say(f"Prepared {len(unique_tracks)} unique tracks for transfer.")
        if args.dry_run:
            for track in unique_tracks[:25]:
                say(f"[dry-run] {track.video_id} {track.title or ''}".rstrip())
            if len(unique_tracks) > 25:
                say(f"[dry-run] ...and {len(unique_tracks) - 25} more")
            report.finished_at = utc_now()
            write_report(args.report, report)
            say(f"Wrote report: {args.report}")
            return 0

        require_write_confirmation(args)
        video_ids = [track.video_id for track in unique_tracks]

        if not args.no_like:
            like_tracks_on_target(
                target,
                unique_tracks,
                report,
                args.sleep,
                args.fail_fast,
                args.rate_limit_retries,
                args.rate_limit_initial_backoff,
            )

        if args.playlist_title:
            create_playlist_on_target(
                target=target,
                video_ids=video_ids,
                title=args.playlist_title,
                description=args.playlist_description,
                privacy=args.playlist_privacy,
                chunk_size=args.chunk_size,
                sleep_seconds=args.sleep,
                report=report,
                fail_fast=args.fail_fast,
                max_retries=args.rate_limit_retries,
                initial_backoff=args.rate_limit_initial_backoff,
            )

    report.finished_at = utc_now()
    write_report(args.report, report)
    say(f"Wrote report: {args.report}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        say("Cancelled. No further changes will be made.", error=True)
        raise SystemExit(130)
    except SystemExit as exc:
        if isinstance(exc.code, str):
            say(f"Error: {human_error(exc)}", error=True)
            raise SystemExit(2)
        raise
    except Exception as exc:
        say(f"Error: {human_error(exc)}", error=True)
        say("Tip: run with --dry-run first, then check transfer_report.json for details.", error=True)
        raise SystemExit(1)
