#!/usr/bin/env python3
"""Transfer YouTube Music liked songs from one account to another.

This script uses ytmusicapi authentication files:
- source account: reads the special "Liked Songs" playlist
- target account: thumbs-up the same video IDs, and optionally creates a playlist

OAuth token files are recommended. Browser-header JSON files remain supported.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ytmusicapi import OAuthCredentials, YTMusic
from ytmusicapi.models.content.enums import LikeStatus


DEFAULT_SOURCE_AUTH = "auth/account_a.json"
DEFAULT_TARGET_AUTH = "auth/account_b.json"
DEFAULT_REPORT = "transfer_report.json"
DEFAULT_ENV_FILE = ".env"


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
        description="Transfer all liked songs from YouTube Music account A to account B."
    )
    parser.add_argument(
        "--source-auth",
        default=DEFAULT_SOURCE_AUTH,
        help=f"ytmusicapi browser auth JSON for account A. Default: {DEFAULT_SOURCE_AUTH}",
    )
    parser.add_argument(
        "--target-auth",
        default=DEFAULT_TARGET_AUTH,
        help=f"ytmusicapi browser auth JSON for account B. Default: {DEFAULT_TARGET_AUTH}",
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


def get_liked_video_ids(client: YTMusic, limit: int | None) -> set[str]:
    tracks = extract_tracks(client.get_liked_songs(limit=limit))
    return {track.video_id for track in tracks}


def write_report(path: str, report: TransferReport) -> None:
    payload = asdict(report)
    report_path = Path(path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def like_tracks_on_target(
    target: YTMusic,
    tracks: list[Track],
    report: TransferReport,
    sleep_seconds: float,
    fail_fast: bool,
) -> None:
    for index, track in enumerate(tracks, start=1):
        try:
            target.rate_song(track.video_id, LikeStatus.LIKE)
            report.liked_on_target += 1
            print(f"[like {index}/{len(tracks)}] {track.video_id} {track.title or ''}".rstrip())
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
        except Exception as exc:  # ytmusicapi raises several request/user/server exceptions.
            failure = {"action": "like", "track": asdict(track), "error": repr(exc)}
            report.failed = report.failed or []
            report.failed.append(failure)
            print(f"[failed like] {track.video_id}: {exc}", file=sys.stderr)
            if fail_fast:
                raise


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
) -> None:
    if not video_ids:
        return

    chunks = iter(chunked(video_ids, chunk_size))
    first_chunk = next(chunks)
    try:
        playlist_id = target.create_playlist(
            title=title,
            description=description,
            privacy_status=privacy,
            video_ids=first_chunk,
        )
        if not isinstance(playlist_id, str):
            raise RuntimeError(f"Unexpected create_playlist response: {playlist_id!r}")
        report.playlist_id = playlist_id
        report.playlist_added += len(first_chunk)
        print(f"[playlist] created {playlist_id} with {len(first_chunk)} tracks")
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    except Exception as exc:
        failure = {"action": "create_playlist", "title": title, "error": repr(exc)}
        report.failed = report.failed or []
        report.failed.append(failure)
        print(f"[failed playlist create] {exc}", file=sys.stderr)
        if fail_fast:
            raise
        return

    for index, video_id_chunk in enumerate(chunks, start=2):
        try:
            target.add_playlist_items(report.playlist_id, videoIds=video_id_chunk, duplicates=False)
            report.playlist_added += len(video_id_chunk)
            print(f"[playlist chunk {index}] added {len(video_id_chunk)} tracks")
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
        except Exception as exc:
            failure = {
                "action": "add_playlist_items",
                "playlist_id": report.playlist_id,
                "video_ids": video_id_chunk,
                "error": repr(exc),
            }
            report.failed = report.failed or []
            report.failed.append(failure)
            print(f"[failed playlist add] chunk {index}: {exc}", file=sys.stderr)
            if fail_fast:
                raise


def main() -> int:
    args = parse_args()
    require_file(args.source_auth)
    require_file(args.target_auth)

    report = TransferReport(
        started_at=utc_now(),
        source_auth=args.source_auth,
        target_auth=args.target_auth,
        limit=args.limit,
        dry_run=args.dry_run,
        skipped=[],
        failed=[],
    )

    source = build_client(args.source_auth, args.auth_mode, args.oauth_client_id, args.oauth_client_secret)
    target = build_client(args.target_auth, args.auth_mode, args.oauth_client_id, args.oauth_client_secret)

    print("Reading liked songs from account A...")
    source_tracks = extract_tracks(source.get_liked_songs(limit=args.limit))
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
        print("Reading existing liked songs from account B...")
        target_likes = get_liked_video_ids(target, limit=None)
        before = len(unique_tracks)
        unique_tracks = [track for track in unique_tracks if track.video_id not in target_likes]
        report.already_liked_on_target = before - len(unique_tracks)

    if not unique_tracks:
        print("No tracks to transfer after filtering.")
        report.finished_at = utc_now()
        write_report(args.report, report)
        return 0

    print(f"Prepared {len(unique_tracks)} unique tracks for transfer.")
    if args.dry_run:
        for track in unique_tracks[:25]:
            print(f"[dry-run] {track.video_id} {track.title or ''}".rstrip())
        if len(unique_tracks) > 25:
            print(f"[dry-run] ...and {len(unique_tracks) - 25} more")
        report.finished_at = utc_now()
        write_report(args.report, report)
        print(f"Wrote report: {args.report}")
        return 0

    video_ids = [track.video_id for track in unique_tracks]

    if not args.no_like:
        like_tracks_on_target(target, unique_tracks, report, args.sleep, args.fail_fast)

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
        )

    report.finished_at = utc_now()
    write_report(args.report, report)
    print(f"Wrote report: {args.report}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
