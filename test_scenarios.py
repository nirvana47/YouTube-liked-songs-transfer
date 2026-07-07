#!/usr/bin/env python3
"""Headless scenario tests for the Liked Songs Transfer desktop app.

Run with::

    QT_QPA_PLATFORM=offscreen python3 tests/test_scenarios.py

No external test framework is required. Each ``test_*`` function uses plain
asserts; ``main()`` runs them all and prints a PASS/FAIL summary, exiting non-zero
if anything fails.

The tests drive the *real* worker functions (``_preview_worker``,
``_transfer_worker``, ``_create_playlist``) and the OAuth ``oauth_flow`` state
machine against the ``mock_backend`` fault-injection presets — no network or real
credentials are used.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Make the repo root importable when run as `python3 tests/test_scenarios.py`.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

import browser_auth  # noqa: E402
import gui_transfer  # noqa: E402
import mock_backend  # noqa: E402
from errors import friendly_error, setup_run_logger  # noqa: E402
from gui_transfer import (  # noqa: E402
    WorkerSignals,
    _create_playlist,
    _preview_worker,
    _transfer_worker,
)
from oauth_flow import (  # noqa: E402
    OAuthSignInEngine,
    PollState,
    account_file_status,
)

# Speed up retries for the tests.
gui_transfer.DEFAULT_RETRIES = 3
gui_transfer.DEFAULT_BACKOFF = 0.001

_APP = QApplication.instance() or QApplication(sys.argv)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class Collector:
    """Capture everything a worker emits through its signals."""

    def __init__(self, signals: WorkerSignals) -> None:
        self.logs: list[str] = []
        self.progress: list[tuple[int, int]] = []
        self.counts: list[str] = []
        self.errors: list[str] = []
        self.finished: list[object] = []
        signals.log.connect(self.logs.append)
        signals.progress.connect(lambda d, t: self.progress.append((d, t)))
        signals.counts.connect(self.counts.append)
        signals.error.connect(self.errors.append)
        signals.finished.connect(self.finished.append)

    @property
    def retried(self) -> bool:
        return any("retry" in line.lower() or "retrying" in line.lower() for line in self.logs)

    @property
    def max_progress(self) -> int:
        return max((d for d, _ in self.progress), default=0)

    @property
    def total(self) -> int:
        return max((t for _, t in self.progress), default=0)


def base_cfg(**overrides) -> dict:
    cfg = dict(
        do_like=True,
        do_playlist=False,
        skip_existing=True,
        skip_unavailable=True,
        limit=None,
        sleep=0,
        chunk_size=50,
        fail_fast=False,
        playlist_title="Demo playlist",
        playlist_privacy="PRIVATE",
        playlist_description="test",
        auth_mode="mock",
        source_auth="mock",
        target_auth="mock",
    )
    cfg.update(overrides)
    return cfg


def new_signals() -> tuple[WorkerSignals, Collector]:
    signals = WorkerSignals()
    return signals, Collector(signals)


def make_report(preview: dict | None = None):
    report = gui_transfer.core.TransferReport(
        started_at=gui_transfer.core.utc_now(),
        source_auth="mock",
        target_auth="mock",
        limit=None,
        dry_run=False,
        skipped=[],
        failed=[],
    )
    if preview:
        report.total_source_tracks = preview.get("total_source", 0)
        report.unique_video_ids = preview.get("unique_count", 0)
        report.already_liked_on_target = preview.get("already_liked", 0)
    return report


def assert_no_raw_dump(text: str) -> None:
    lowered = text.lower()
    assert "traceback" not in lowered, f"friendly text leaked a traceback: {text!r}"
    assert "  file \"" not in lowered, f"friendly text leaked a stack frame: {text!r}"


def run_preview(scenario: str, cfg: dict) -> tuple[dict, Collector]:
    source, target = gui_transfer._demo_clients(scenario)
    signals, collector = new_signals()
    result = _preview_worker(signals, cfg, source, target)
    return result, collector


# ---------------------------------------------------------------------------
# Scenario tests
# ---------------------------------------------------------------------------
def test_happy():
    cfg = base_cfg()
    result, _ = run_preview("happy", cfg)
    assert result["total_source"] == 6
    assert len(result["unique"]) == 6, result

    source, target = gui_transfer._demo_clients("happy")
    signals, collector = new_signals()
    report = make_report(result)
    _transfer_worker(signals, cfg, target, result["unique"], report)
    assert report.liked_on_target == 6, report.liked_on_target
    assert not (report.failed or []), report.failed
    assert collector.max_progress == collector.total == 6, collector.progress
    assert getattr(report, "report_path", None) and Path(report.report_path).is_file()


def test_empty_liked():
    cfg = base_cfg()
    result, _ = run_preview("empty_liked", cfg)
    assert result["total_source"] == 0
    assert len(result["unique"]) == 0


def test_some_unavailable():
    cfg = base_cfg()
    result, _ = run_preview("some_unavailable", cfg)
    assert result["unavailable_skipped"] == 2, result
    assert len(result["unique"]) == 4, result


def test_rate_limit_midway():
    cfg = base_cfg()
    result, _ = run_preview("rate_limit_midway", cfg)
    assert len(result["unique"]) == 5

    source, target = gui_transfer._demo_clients("rate_limit_midway")
    signals, collector = new_signals()
    report = make_report(result)
    _transfer_worker(signals, cfg, target, result["unique"], report)
    # All songs eventually liked; the burst of 429s was retried.
    assert report.liked_on_target == 5, report.liked_on_target
    assert not (report.failed or []), report.failed
    assert collector.retried, "expected retry/backoff log lines"
    assert target.rate_calls > 5, target.rate_calls  # retries happened
    assert collector.max_progress == collector.total == 5


def test_network_timeout():
    cfg = base_cfg()
    source, target = gui_transfer._demo_clients("network_timeout")
    signals, collector = new_signals()
    raised = None
    try:
        _preview_worker(signals, cfg, source, target)
    except Exception as exc:  # noqa: BLE001
        raised = exc
    assert raised is not None, "network_timeout should raise after retries"
    assert collector.retried, "expected retry attempts before giving up"
    message = friendly_error(raised)
    assert_no_raw_dump(message)
    assert "connection" in message.lower() or "reach" in message.lower(), message


def test_partial_failures():
    cfg = base_cfg()
    result, _ = run_preview("partial_failures", cfg)
    assert len(result["unique"]) == 5

    source, target = gui_transfer._demo_clients("partial_failures")
    signals, collector = new_signals()
    report = make_report(result)
    _transfer_worker(signals, cfg, target, result["unique"], report)
    assert report.liked_on_target == 3, report.liked_on_target
    failed = report.failed or []
    assert len(failed) == 2, failed
    for item in failed:
        assert_no_raw_dump(item["error"])
    # Progress advances on failure too, and reaches the max exactly.
    assert collector.max_progress == collector.total == 5, collector.progress
    assert getattr(report, "report_path", None) and Path(report.report_path).is_file()


def test_playlist_fail():
    cfg = base_cfg(do_playlist=True)
    result, _ = run_preview("playlist_fail", cfg)
    assert len(result["unique"]) == 4

    source, target = gui_transfer._demo_clients("playlist_fail")
    signals, collector = new_signals()
    report = make_report(result)
    _transfer_worker(signals, cfg, target, result["unique"], report)
    # Likes succeeded; playlist creation failed -> partial success, no crash.
    assert report.liked_on_target == 4, report.liked_on_target
    failed = report.failed or []
    assert any(item.get("action") == "create_playlist" for item in failed), failed
    for item in failed:
        assert_no_raw_dump(item["error"])
    # total = 4 likes + 1 playlist batch; bar still completes.
    assert collector.total == 5, collector.progress
    assert collector.max_progress == 5, collector.progress


def test_bad_auth():
    cfg = base_cfg()
    source, target = gui_transfer._demo_clients("bad_auth")
    signals, _ = new_signals()
    raised = None
    try:
        _preview_worker(signals, cfg, source, target)
    except Exception as exc:  # noqa: BLE001
        raised = exc
    assert raised is not None
    message = friendly_error(raised)
    assert_no_raw_dump(message)
    assert "sign in" in message.lower() or "sign-in" in message.lower() or "account" in message.lower(), message


def test_expired_token_backend():
    cfg = base_cfg()
    source, target = gui_transfer._demo_clients("expired_token")
    signals, _ = new_signals()
    raised = None
    try:
        _preview_worker(signals, cfg, source, target)
    except Exception as exc:  # noqa: BLE001
        raised = exc
    assert raised is not None
    message = friendly_error(raised)
    assert_no_raw_dump(message)


def test_cancellation():
    cfg = base_cfg()
    result, _ = run_preview("happy", cfg)
    source, target = gui_transfer._demo_clients("happy")
    signals, collector = new_signals()
    report = make_report(result)
    cancel_event = threading.Event()

    # Cancel right after the first like is processed.
    def maybe_cancel(done, total):
        if done >= 1:
            cancel_event.set()

    signals.progress.connect(maybe_cancel)
    _transfer_worker(signals, cfg, target, result["unique"], report, cancel_event=cancel_event)

    assert getattr(report, "cancelled", False) is True, "report should be marked cancelled"
    assert 1 <= report.liked_on_target < 5, report.liked_on_target
    # A partial report is still written.
    assert getattr(report, "report_path", None) and Path(report.report_path).is_file()
    # Cancelled runs do not force the bar to 100%.
    assert collector.max_progress < collector.total or collector.total == 0, collector.progress
    assert any("cancel" in line.lower() for line in collector.logs), collector.logs


def test_playlist_create_direct():
    # Direct exercise of _create_playlist with a happy target.
    _, target = gui_transfer._demo_clients("happy")
    cfg = base_cfg(do_playlist=True, chunk_size=2)
    signals, _ = new_signals()
    report = make_report()
    ids = [f"vid{i:03d}" for i in range(1, 6)]  # 5 ids -> 3 chunks of 2
    advanced = {"n": 0}
    _create_playlist(signals, cfg, target, ids, report, advance=lambda: advanced.__setitem__("n", advanced["n"] + 1))
    assert report.playlist_id and report.playlist_id.startswith("PL_MOCK_")
    assert report.playlist_added == 5, report.playlist_added
    assert advanced["n"] == 3, advanced


def test_oauth_pending_then_success(tmp_token: Path):
    engine = OAuthSignInEngine(
        "cid", "secret", str(tmp_token),
        oauth_factory=mock_backend.fake_oauth_factory("success", pending_times=2),
        store_token=mock_backend.store_token_json,
    )
    code = engine.begin()
    assert code["user_code"] == "ABCD-EFGH"
    assert engine.user_code in engine.verification_url
    states = []
    for _ in range(5):
        result = engine.poll_once()
        states.append(result.state)
        if result.is_final:
            break
    assert states[0] == PollState.PENDING
    assert states[1] == PollState.PENDING
    assert states[-1] == PollState.SUCCESS, states
    assert tmp_token.is_file()
    status, _ = account_file_status(str(tmp_token))
    assert status == "ok", status


def test_oauth_denied(tmp_token: Path):
    engine = OAuthSignInEngine(
        "cid", "secret", str(tmp_token),
        oauth_factory=mock_backend.fake_oauth_factory("denied"),
        store_token=mock_backend.store_token_json,
    )
    engine.begin()
    result = engine.poll_once()
    assert result.state == PollState.DENIED, result
    assert result.is_final
    assert_no_raw_dump(result.message)
    assert not tmp_token.is_file()


def test_oauth_expired(tmp_token: Path):
    engine = OAuthSignInEngine(
        "cid", "secret", str(tmp_token),
        oauth_factory=mock_backend.fake_oauth_factory("expired"),
        store_token=mock_backend.store_token_json,
    )
    engine.begin()
    result = engine.poll_once()
    assert result.state == PollState.EXPIRED, result
    assert result.is_final
    assert_no_raw_dump(result.message)


SAMPLE_CURL = (
    "curl 'https://music.youtube.com/youtubei/v1/browse?prettyPrint=false' \\\n"
    "  -H 'accept: */*' \\\n"
    "  -H 'authorization: SAPISIDHASH 1699_abcdef' \\\n"
    "  -H 'cookie: VISITOR_INFO1_LIVE=xyz; __Secure-3PAPISID=abc/DEF; SID=123' \\\n"
    "  -H 'x-goog-authuser: 0' \\\n"
    "  -H 'sec-ch-ua: chromium' \\\n"
    "  -H 'user-agent: Mozilla/5.0 (Macintosh)' \\\n"
    "  --data-raw '{\"context\":{}}' \\\n"
    "  --compressed"
)


def test_curl_parse_and_save(tmp_token: Path):
    dest = tmp_token.parent / "source_headers.json"
    final = browser_auth.save_browser_headers(SAMPLE_CURL, str(dest))
    # sec-* headers must be dropped; cookie/account headers preserved. ytmusicapi
    # computes the SAPISIDHASH authorization header dynamically from the cookie.
    assert not any(k.startswith("sec") for k in final), final
    assert "__Secure-3PAPISID" in final["cookie"]
    assert final["x-goog-authuser"] == "0"
    # File is valid and recognised as a browser session.
    status, _ = browser_auth.browser_file_status(str(dest))
    assert status == "ok", status
    # And ytmusicapi loads it as browser auth.
    from ytmusicapi import YTMusic
    from ytmusicapi.auth.types import AuthType
    assert YTMusic(str(dest)).auth_type == AuthType.BROWSER

    no_auth_dest = tmp_token.parent / "source_headers_no_auth.json"
    no_auth = SAMPLE_CURL.replace("  -H 'authorization: SAPISIDHASH 1699_abcdef' \\\n", "")
    assert browser_auth.save_browser_headers(no_auth, str(no_auth_dest))["cookie"]
    assert browser_auth.browser_file_status(str(no_auth_dest))[0] == "ok"
    assert YTMusic(str(no_auth_dest)).auth_type == AuthType.BROWSER


def test_curl_parse_errors():
    cases = {
        "empty": "",
        "not_curl": "hello world",
        "wrong_domain": "curl 'https://example.com/x' -H 'cookie: __Secure-3PAPISID=a'",
        "no_cookie": "curl 'https://music.youtube.com/x' -H 'x-goog-authuser: 0'",
        "cookie_missing_token": "curl 'https://music.youtube.com/x' -H 'cookie: SID=1'",
    }
    for name, text in cases.items():
        raised = None
        try:
            browser_auth.save_browser_headers(text, "/tmp/should_not_write.json")
        except browser_auth.CurlParseError as exc:
            raised = exc
        assert raised is not None, f"expected CurlParseError for {name}"
        assert_no_raw_dump(str(raised))
        assert str(raised).strip(), name


def test_browser_status_missing_and_invalid(tmp_token: Path):
    missing, _ = browser_auth.browser_file_status(str(tmp_token))
    assert missing == "missing"
    bad = tmp_token.parent / "bad_headers.json"
    bad.write_text('{"cookie": "SID=1"}', encoding="utf-8")  # no SAPISID
    status, _ = browser_auth.browser_file_status(str(bad))
    assert status == "invalid", status


def test_browser_signin_section_flow():
    wizard = gui_transfer.build_wizard(demo_mode=False)
    auth = wizard.page(gui_transfer.PAGE_AUTH)
    try:
        assert auth.current_method() == "browser"  # recommended default
        src = auth.source_section
        assert src.mode == "browser"
        src.curl_edit.setPlainText(SAMPLE_CURL)
        src._save_browser_headers()
        assert src.ready, src.browser_result.text()
        cfg = auth._collect_config()
        assert cfg["auth_mode"] == "browser"
        assert cfg["source_auth"].endswith("source_headers.json")
        # Switching to OAuth flips the mode and config.
        auth.method_oauth_radio.setChecked(True)
        assert auth.current_method() == "oauth"
        assert src.mode == "oauth"
        assert auth._collect_config()["auth_mode"] == "oauth"
    finally:
        for section in (auth.source_section, auth.target_section):
            p = Path(section.headers_path)
            if p.is_file():
                p.unlink()
        wizard.close()


def test_account_file_status(tmp_token: Path):
    missing, _ = account_file_status(str(tmp_token))
    assert missing == "missing"
    bad = tmp_token.parent / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    status, _ = account_file_status(str(bad))
    assert status == "invalid"


def test_logging_creates_file():
    logger, log_path = setup_run_logger("test")
    cfg = base_cfg()
    result, _ = run_preview("happy", cfg)
    _, target = gui_transfer._demo_clients("happy")
    signals, _ = new_signals()
    report = make_report(result)
    _transfer_worker(signals, cfg, target, result["unique"], report, logger=logger)
    for handler in list(logger.handlers):
        handler.flush()
    assert Path(log_path).is_file(), log_path
    content = Path(log_path).read_text(encoding="utf-8")
    assert "Transfer start" in content, content[:500]


def test_demo_wizard_constructs():
    wizard = gui_transfer.build_wizard(demo_mode=True)
    assert "DEMO MODE" in wizard.windowTitle()
    assert wizard.button(gui_transfer.QWizard.CustomButton1).text() == "Quit"
    assert wizard.button(gui_transfer.QWizard.CustomButton2).text() == "Clean Up"
    wizard.close()


def test_inline_demo_button_switches_normal_wizard():
    wizard = gui_transfer.build_wizard(demo_mode=False)
    auth = wizard.page(gui_transfer.PAGE_AUTH)
    assert hasattr(auth, "inline_demo_btn")
    auth.inline_demo_combo.setCurrentIndex(0)
    auth._start_inline_demo()
    assert wizard.demo_mode is True
    assert auth.isComplete()
    assert "DEMO MODE" in wizard.windowTitle()
    wizard.close()


def test_cleanup_removes_local_runtime_files():
    import tempfile

    original_script_dir = gui_transfer.SCRIPT_DIR
    with tempfile.TemporaryDirectory(prefix="ls-cleanup-") as tmp:
        root = Path(tmp)
        gui_transfer.SCRIPT_DIR = root
        (root / gui_transfer.APP_VENV_NAME).mkdir()
        (root / gui_transfer.APP_VENV_NAME / "marker.txt").write_text("x", encoding="utf-8")
        (root / ".env").write_text("SECRET=1", encoding="utf-8")
        (root / ".env.example").write_text("example", encoding="utf-8")
        (root / "auth").mkdir()
        (root / "auth" / "account_a_oauth.json").write_text("{}", encoding="utf-8")
        (root / "client_secret_test.json").write_text("{}", encoding="utf-8")
        deleted = gui_transfer.perform_cleanup(logger=None)
        assert gui_transfer.APP_VENV_NAME in deleted
        assert not (root / gui_transfer.APP_VENV_NAME).exists()
        assert not (root / ".env").exists()
        assert (root / ".env.example").exists()
        assert not (root / "auth" / "account_a_oauth.json").exists()
        assert not (root / "client_secret_test.json").exists()
    gui_transfer.SCRIPT_DIR = original_script_dir


def test_friendly_errors_no_dump():
    samples = [
        mock_backend.MockRateLimitError(),
        mock_backend.MockNetworkTimeout("timed out"),
        mock_backend.MockAuthError(),
        FileNotFoundError("auth/x.json"),
        KeyError("videoId"),
        ValueError("bad json"),
        RuntimeError("boom"),
    ]
    for exc in samples:
        message = friendly_error(exc)
        assert_no_raw_dump(message)
        assert message.strip(), exc


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def _make_tmp_token(index: int) -> Path:
    import tempfile

    d = Path(tempfile.mkdtemp(prefix=f"ls-oauth-{index}-"))
    return d / "token.json"


def main() -> int:
    tests = [
        test_happy,
        test_empty_liked,
        test_some_unavailable,
        test_rate_limit_midway,
        test_network_timeout,
        test_partial_failures,
        test_playlist_fail,
        test_bad_auth,
        test_expired_token_backend,
        test_cancellation,
        test_playlist_create_direct,
        test_oauth_pending_then_success,
        test_oauth_denied,
        test_oauth_expired,
        test_curl_parse_and_save,
        test_curl_parse_errors,
        test_browser_status_missing_and_invalid,
        test_browser_signin_section_flow,
        test_account_file_status,
        test_logging_creates_file,
        test_demo_wizard_constructs,
        test_inline_demo_button_switches_normal_wizard,
        test_cleanup_removes_local_runtime_files,
        test_friendly_errors_no_dump,
    ]
    passed = 0
    failed = 0
    for index, test in enumerate(tests):
        name = test.__name__
        try:
            if "tmp_token" in test.__code__.co_varnames[: test.__code__.co_argcount]:
                test(_make_tmp_token(index))
            else:
                test()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback

            print(f"ERROR {name}: {exc}")
            traceback.print_exc()
        else:
            passed += 1
            print(f"PASS  {name}")
    print("-" * 50)
    print(f"{passed} passed, {failed} failed, {passed + failed} total")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
