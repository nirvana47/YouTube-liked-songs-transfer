#!/usr/bin/env python3
"""Graphical wizard for transferring YouTube Music liked songs between accounts.

This is a friendly, click-through front end for ``transfer_liked_songs.py``. It is
designed for personal use on macOS: double-click the ``Transfer Liked Songs.command``
launcher and follow the five steps.

    Connect  ->  Choose what to transfer  ->  Preview  ->  Transfer  ->  Done

The whole OAuth sign-in happens *inside* the app (Step 1) — no terminal required.
It reuses the stable, well-tested helpers from ``transfer_liked_songs`` (client
building, reading liked songs, de-duplication, reporting) and drives the actual write
operations itself so it can show live progress. Nothing is written to the target
account until you explicitly click "Start transfer" on the transfer step.

Errors are always shown as short, friendly messages; full technical detail is written
to a per-run log file under ``logs/``. A ``--demo`` mode (or ``LSTRANSFER_DEMO=1``)
lets you preview every success/error/exit path using a built-in mock backend, without
real credentials.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Optional

# Make sure the repository directory is importable regardless of the working directory.
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from PySide6.QtCore import Qt, QObject, QRunnable, QThreadPool, QUrl, Signal, Slot
    from PySide6.QtGui import QDesktopServices, QFont
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QFileDialog,
        QFormLayout,
        QGroupBox,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QPlainTextEdit,
        QProgressBar,
        QPushButton,
        QSpinBox,
        QTableWidget,
        QTableWidgetItem,
        QVBoxLayout,
        QWidget,
        QWizard,
        QWizardPage,
    )
except ImportError:  # pragma: no cover - guidance for a missing GUI toolkit.
    sys.stderr.write(
        "\nPySide6 is required to run the graphical wizard.\n"
        "Install it with:\n\n"
        "    python3 -m pip install PySide6-Essentials\n\n"
        "Or just use the double-click launcher: 'Transfer Liked Songs.command'.\n\n"
    )
    raise

# Core logic lives in the CLI module. Only stable, fundamental helpers are used here.
import transfer_liked_songs as core
from ytmusicapi.models.content.enums import LikeStatus

# Local, Qt-free support modules.
import mock_backend
from errors import classify_retryable, friendly_error, setup_run_logger
from oauth_flow import (
    GOOGLE_CLOUD_CONSOLE_URL,
    OAuthSignInEngine,
    PollState,
    account_file_status,
)


# ---------------------------------------------------------------------------
# Demo-mode detection
# ---------------------------------------------------------------------------
def is_demo_mode(argv: Optional[list[str]] = None) -> bool:
    argv = list(sys.argv if argv is None else argv)
    if str(os.getenv("LSTRANSFER_DEMO", "")).strip().lower() in ("1", "true", "yes", "on"):
        return True
    return "--demo" in argv


# ---------------------------------------------------------------------------
# Background worker plumbing (Qt threads talk to the UI only through signals).
# ---------------------------------------------------------------------------
class WorkerSignals(QObject):
    log = Signal(str)
    progress = Signal(int, int)  # done, total
    counts = Signal(str)         # running counts / phase label
    info = Signal(object)        # structured payloads (e.g. OAuth code details)
    finished = Signal(object)
    error = Signal(str)


class Worker(QRunnable):
    """Runs a blocking function off the UI thread and reports back via signals."""

    def __init__(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()
        self.logger: Any = None

    @Slot()
    def run(self) -> None:  # pragma: no cover - exercised only via the live UI.
        try:
            result = self.fn(self.signals, *self.args, **self.kwargs)
        except BaseException as exc:  # noqa: BLE001 - convert to a friendly message.
            if self.logger is not None:
                try:
                    self.logger.error("Worker %s failed", getattr(self.fn, "__name__", "?"), exc_info=True)
                except Exception:
                    pass
            self.signals.error.emit(friendly_error(exc))
        else:
            self.signals.finished.emit(result)


# ---------------------------------------------------------------------------
# Shared configuration carried across wizard pages.
# ---------------------------------------------------------------------------
DEFAULT_RETRIES = getattr(core, "DEFAULT_RATE_LIMIT_RETRIES", 5)
DEFAULT_BACKOFF = getattr(core, "DEFAULT_RATE_LIMIT_INITIAL_BACKOFF", 2.0)
KEYRING_SERVICE = getattr(core, "DEFAULT_KEYRING_SERVICE", "YouTube-liked-songs-transfer")

# The GUI prefers the hardening helpers from the CLI module when they are available,
# but degrades gracefully so it also works against the plain baseline module.
_CORE_RETRY = getattr(core, "call_with_rate_limit_retry", None)
_OAUTH_KEYRING = getattr(core, "OAuthKeyringAuthFile", None)


def _make_on_retry(signals: Optional[WorkerSignals], logger: Any) -> Callable[[int, float, BaseException], None]:
    """Build an ``on_retry`` callback that logs to the UI and the run log."""

    def on_retry(attempt: int, wait: float, exc: BaseException) -> None:
        message = f"Rate limited / network issue — retrying in {wait:.1f}s (attempt {attempt})."
        if signals is not None:
            try:
                signals.log.emit(message)
            except Exception:
                pass
        if logger is not None:
            try:
                logger.warning("Retry %d after %.2fs due to %r", attempt, wait, exc)
            except Exception:
                pass

    return on_retry


def call_api(
    name: str,
    func: Callable[[], Any],
    *,
    on_retry: Optional[Callable[[int, float, BaseException], None]] = None,
    logger: Any = None,
) -> Any:
    """Call a ytmusicapi method, retrying on rate-limit AND transient network errors.

    Uses exponential backoff. ``on_retry(attempt, wait, exc)`` is invoked before each
    wait so callers can log "retrying in Xs".

    When the hardened ``core.call_with_rate_limit_retry`` helper is present and the
    caller does not need per-retry logging (no ``on_retry`` / ``logger``), we defer to
    it. Otherwise we run a local loop that additionally covers transient network errors
    (which the core helper re-raises) and emits per-retry callbacks.
    """
    retries = int(DEFAULT_RETRIES)
    backoff = float(DEFAULT_BACKOFF)

    if on_retry is None and logger is None and _CORE_RETRY is not None:
        return _CORE_RETRY(name, func, retries, backoff)

    attempts = max(0, retries) + 1
    last_exc: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except Exception as exc:  # noqa: BLE001 - retry only when it makes sense.
            last_exc = exc
            if attempt >= attempts or not classify_retryable(exc):
                raise
            wait = backoff * (2 ** (attempt - 1))
            if on_retry is not None:
                try:
                    on_retry(attempt, wait, exc)
                except Exception:
                    pass
            if logger is not None:
                try:
                    logger.warning(
                        "Retryable error on %s (attempt %d/%d), waiting %.2fs: %r",
                        name, attempt, attempts - 1, wait, exc,
                    )
                except Exception:
                    pass
            time.sleep(wait)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"Unreachable retry state for {name}")


def _target_liked_ids(target: Any, on_retry: Any = None, logger: Any = None) -> set:
    """Return the set of video IDs already liked on the target account."""
    payload = call_api(
        "get_liked_songs",
        lambda: target.get_liked_songs(limit=None),
        on_retry=on_retry,
        logger=logger,
    )
    return {t.video_id for t in core.extract_tracks(payload)}


PAGE_AUTH = 0
PAGE_OPTIONS = 1
PAGE_PREVIEW = 2
PAGE_TRANSFER = 3
PAGE_DONE = 4

SOURCE_TOKEN_NAME = "account_a_oauth.json"
TARGET_TOKEN_NAME = "account_b_oauth.json"


def default_auth_path(name: str) -> str:
    return str(SCRIPT_DIR / "auth" / name)


def _interruptible_sleep(seconds: float, cancel_event: Optional[threading.Event]) -> None:
    """Sleep in small steps so cancellation is responsive."""
    if seconds <= 0:
        return
    remaining = float(seconds)
    step = 0.25
    while remaining > 0:
        if cancel_event is not None and cancel_event.is_set():
            return
        time.sleep(min(step, remaining))
        remaining -= step


# ---------------------------------------------------------------------------
# Step 1: Connect your accounts (full inline OAuth)
# ---------------------------------------------------------------------------
class _AccountSection:
    """UI + state for one account's inline OAuth sign-in."""

    def __init__(self, page: "AuthPage", label: str, token_path: str) -> None:
        self.page = page
        self.label = label
        self.token_path = token_path
        self.ready = False
        self.cancel_event: Optional[threading.Event] = None

        self.box = QGroupBox(label)
        outer = QVBoxLayout(self.box)

        status_row = QHBoxLayout()
        self.status_label = QLabel("Checking…")
        self.status_label.setWordWrap(True)
        self.signin_btn = QPushButton("Sign in")
        self.signin_btn.clicked.connect(self.start_signin)
        status_row.addWidget(self.status_label, 1)
        status_row.addWidget(self.signin_btn)
        outer.addLayout(status_row)

        # The live sign-in panel (hidden until the user clicks Sign in).
        self.panel = QWidget()
        panel_layout = QVBoxLayout(self.panel)
        panel_layout.setContentsMargins(0, 4, 0, 0)
        self.instruction = QLabel("Starting sign-in…")
        self.instruction.setWordWrap(True)
        panel_layout.addWidget(self.instruction)

        self.code_label = QLabel("")
        code_font = QFont()
        code_font.setPointSize(code_font.pointSize() + 6)
        code_font.setBold(True)
        self.code_label.setFont(code_font)
        self.code_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        panel_layout.addWidget(self.code_label)

        self.url_label = QLabel("")
        self.url_label.setOpenExternalLinks(True)
        self.url_label.setWordWrap(True)
        panel_layout.addWidget(self.url_label)

        btn_row = QHBoxLayout()
        self.open_btn = QPushButton("Open browser")
        self.open_btn.clicked.connect(self._open_browser)
        self.open_btn.setEnabled(False)
        self.retry_btn = QPushButton("Try again")
        self.retry_btn.clicked.connect(self.start_signin)
        self.retry_btn.setVisible(False)
        btn_row.addWidget(self.open_btn)
        btn_row.addWidget(self.retry_btn)
        btn_row.addStretch(1)
        panel_layout.addLayout(btn_row)

        self.spinner = QProgressBar()
        self.spinner.setRange(0, 0)  # indeterminate "busy" spinner
        self.spinner.setVisible(False)
        panel_layout.addWidget(self.spinner)

        self.poll_label = QLabel("")
        self.poll_label.setWordWrap(True)
        panel_layout.addWidget(self.poll_label)

        self.panel.setVisible(False)
        outer.addWidget(self.panel)

        self._verification_url = ""

    # -- status refresh ------------------------------------------------------
    def refresh_status(self) -> None:
        status, message = account_file_status(self.token_path)
        if status == "ok":
            self.ready = True
            self.status_label.setText(f"✓ {message}")
            self.signin_btn.setText("Sign in again")
        else:
            self.ready = False
            self.status_label.setText(message)
            self.signin_btn.setText("Sign in")
        self.panel.setVisible(False)
        self.page.refresh_overall()

    # -- sign-in flow --------------------------------------------------------
    def start_signin(self) -> None:
        client_id, client_secret = self.page.current_credentials()
        if not client_id or not client_secret:
            self.page.show_credentials_needed()
            return
        self.ready = False
        self.page.refresh_overall()
        self.signin_btn.setEnabled(False)
        self.retry_btn.setVisible(False)
        self.panel.setVisible(True)
        self.spinner.setVisible(True)
        self.open_btn.setEnabled(False)
        self.instruction.setText("Requesting a sign-in code from Google…")
        self.code_label.setText("")
        self.url_label.setText("")
        self.poll_label.setText("")

        self.cancel_event = threading.Event()
        engine = OAuthSignInEngine(client_id, client_secret, self.token_path)
        worker = Worker(_oauth_signin_worker, engine, self.cancel_event)
        worker.logger = self.page.tw.logger
        worker.signals.info.connect(self._on_code)
        worker.signals.log.connect(self.poll_label.setText)
        worker.signals.error.connect(self._on_error)
        worker.signals.finished.connect(self._on_finished)
        self.page.tw.pool.start(worker)

    def _on_code(self, info: Any) -> None:
        self._verification_url = info.get("verification_url", "")
        user_code = info.get("user_code", "")
        self.instruction.setText(
            "1) Click “Open browser” (or open the link).  "
            "2) Sign in with the correct Google account and approve.  "
            "You don’t need to come back and click anything — this updates automatically."
        )
        if user_code:
            self.code_label.setText(f"Code: {user_code}")
        if self._verification_url:
            self.url_label.setText(
                f'<a href="{self._verification_url}">{self._verification_url}</a>'
            )
        self.open_btn.setEnabled(bool(self._verification_url))

    def _open_browser(self) -> None:
        if not self._verification_url:
            return
        try:
            opened = QDesktopServices.openUrl(QUrl(self._verification_url))
        except Exception:
            opened = False
        if not opened:
            try:
                webbrowser.open(self._verification_url)
            except Exception:
                pass

    def _on_error(self, message: str) -> None:
        self.spinner.setVisible(False)
        self.signin_btn.setEnabled(True)
        self.retry_btn.setVisible(True)
        self.poll_label.setText(message)

    def _on_finished(self, result: Any) -> None:
        self.spinner.setVisible(False)
        self.signin_btn.setEnabled(True)
        state = (result or {}).get("state")
        if state == "success":
            self.poll_label.setText("Signed in successfully.")
            self.refresh_status()
        elif state == "cancelled":
            self.poll_label.setText("Sign-in cancelled.")
            self.retry_btn.setVisible(True)
        else:
            self.poll_label.setText((result or {}).get("message", "Sign-in did not complete."))
            self.retry_btn.setVisible(True)
        self.page.refresh_overall()

    def cancel(self) -> None:
        if self.cancel_event is not None:
            self.cancel_event.set()


def _oauth_signin_worker(
    signals: WorkerSignals,
    engine: OAuthSignInEngine,
    cancel_event: Optional[threading.Event] = None,
    logger: Any = None,
) -> dict[str, Any]:
    """Run the OAuth device flow: request a code, then auto-poll until final."""
    engine.begin()
    signals.info.emit(
        {
            "verification_url": engine.verification_url,
            "user_code": engine.user_code,
            "interval": engine.interval,
        }
    )
    while True:
        if cancel_event is not None and cancel_event.is_set():
            return {"state": "cancelled"}
        result = engine.poll_once()
        signals.log.emit(result.message)
        if result.is_final:
            if result.state == PollState.SUCCESS:
                return {"state": "success", "token_path": result.token_path}
            state_name = {
                PollState.DENIED: "denied",
                PollState.EXPIRED: "expired",
            }.get(result.state, "error")
            return {"state": state_name, "message": result.message}
        _interruptible_sleep(engine.interval, cancel_event)


class AuthPage(QWizardPage):
    def __init__(self, wizard: "TransferWizard") -> None:
        super().__init__()
        self.tw = wizard
        self._connected = False
        self.setTitle("Step 1 of 5 — Connect your accounts")

        layout = QVBoxLayout(self)

        if self.tw.demo_mode:
            self.setSubTitle("Demo mode: pick a scenario to preview how the app behaves — no sign-in needed.")
            self._build_demo_ui(layout)
        else:
            self.setSubTitle(
                "Sign in to both accounts right here — no terminal needed. "
                "OAuth is the recommended, primary method."
            )
            self._build_oauth_ui(layout)

        layout.addStretch(1)

    # -- Demo UI -------------------------------------------------------------
    def _build_demo_ui(self, layout: QVBoxLayout) -> None:
        box = QGroupBox("Choose a scenario to simulate")
        form = QVBoxLayout(box)
        self.scenario_combo = QComboBox()
        for name in sorted(mock_backend.SCENARIOS):
            self.scenario_combo.addItem(mock_backend.SCENARIO_LABELS.get(name, name), name)
        form.addWidget(self.scenario_combo)
        self.demo_desc = QLabel("")
        self.demo_desc.setWordWrap(True)
        form.addWidget(self.demo_desc)
        self.demo_btn = QPushButton("Use this scenario →")
        self.demo_btn.clicked.connect(self._demo_use)
        form.addWidget(self.demo_btn)
        self.demo_status = QLabel("")
        self.demo_status.setWordWrap(True)
        form.addWidget(self.demo_status)
        layout.addWidget(box)
        self.scenario_combo.currentIndexChanged.connect(self._demo_scenario_changed)
        self._demo_scenario_changed()

    def _demo_scenario_changed(self, *_: Any) -> None:
        self._connected = False
        name = self.scenario_combo.currentData()
        self.demo_desc.setText(mock_backend.SCENARIO_LABELS.get(name, name))
        self.demo_status.setText("")
        self.completeChanged.emit()

    def _demo_use(self) -> None:
        name = self.scenario_combo.currentData()
        self.tw.scenario = name
        source, target = _demo_clients(name)
        self.tw.attach_session(ExitStack(), source, target)
        self.tw.preview_loaded = False
        self._connected = True
        self.demo_status.setText(f"Ready — scenario “{name}”. Click Next to preview it.")
        self.completeChanged.emit()

    # -- OAuth UI ------------------------------------------------------------
    def _build_oauth_ui(self, layout: QVBoxLayout) -> None:
        # Credentials
        self.creds_box = QGroupBox("Google OAuth client credentials (one-time)")
        creds_layout = QVBoxLayout(self.creds_box)
        self.help_label = QLabel(
            "You need a Google OAuth client ID + secret once. In "
            f'<a href="{GOOGLE_CLOUD_CONSOLE_URL}">Google Cloud Console → Credentials</a>, '
            "create an “OAuth client ID” of type “TVs and Limited Input devices”, then paste "
            "the two values below. They are saved to a local <code>.env</code> so you only do this once."
        )
        self.help_label.setOpenExternalLinks(True)
        self.help_label.setWordWrap(True)
        creds_layout.addWidget(self.help_label)

        creds_form = QFormLayout()
        self.client_id_edit = QLineEdit()
        self.client_secret_edit = QLineEdit()
        self.client_secret_edit.setEchoMode(QLineEdit.Password)
        self.client_id_edit.setPlaceholderText("Loaded from .env if present")
        self.client_secret_edit.setPlaceholderText("Loaded from .env if present")
        creds_form.addRow("Client ID:", self.client_id_edit)
        creds_form.addRow("Client secret:", self.client_secret_edit)
        creds_layout.addLayout(creds_form)

        save_row = QHBoxLayout()
        self.save_env_btn = QPushButton("Save to .env")
        self.save_env_btn.clicked.connect(self._save_env)
        self.save_env_status = QLabel("")
        self.save_env_status.setWordWrap(True)
        save_row.addWidget(self.save_env_btn)
        save_row.addWidget(self.save_env_status, 1)
        creds_layout.addLayout(save_row)
        layout.addWidget(self.creds_box)

        # Account sign-in sections
        self.source_section = _AccountSection(self, "Source account (A)", default_auth_path(SOURCE_TOKEN_NAME))
        self.target_section = _AccountSection(self, "Target account (B)", default_auth_path(TARGET_TOKEN_NAME))
        layout.addWidget(self.source_section.box)
        layout.addWidget(self.target_section.box)

        # Verify + status
        verify_row = QHBoxLayout()
        self.verify_btn = QPushButton("Verify accounts & continue")
        self.verify_btn.setEnabled(False)
        self.verify_btn.clicked.connect(self._verify)
        self.overall_label = QLabel("Sign in to both accounts to continue.")
        self.overall_label.setWordWrap(True)
        verify_row.addWidget(self.verify_btn)
        verify_row.addWidget(self.overall_label, 1)
        layout.addLayout(verify_row)

        # Advanced: browser-header files fallback.
        self.advanced_check = QCheckBox("Advanced: use browser-header files instead of OAuth")
        self.advanced_check.toggled.connect(self._toggle_advanced)
        layout.addWidget(self.advanced_check)

        self.browser_box = QGroupBox("Browser-header files (advanced)")
        browser_form = QFormLayout(self.browser_box)
        self.browser_source_edit = QLineEdit(default_auth_path("account_a.json"))
        self.browser_target_edit = QLineEdit(default_auth_path("account_b.json"))
        browser_form.addRow("Source (A):", self._file_row(self.browser_source_edit))
        browser_form.addRow("Target (B):", self._file_row(self.browser_target_edit))
        self.browser_box.setVisible(False)
        layout.addWidget(self.browser_box)

        self._prefill_from_env()

    def _file_row(self, edit: QLineEdit) -> QWidget:
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        browse = QPushButton("Browse…")
        browse.setFixedWidth(90)
        browse.clicked.connect(lambda: self._browse_into(edit))
        row_layout.addWidget(edit, 1)
        row_layout.addWidget(browse)
        return row

    def _browse_into(self, edit: QLineEdit) -> None:
        start_dir = str(SCRIPT_DIR / "auth")
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose an account file", start_dir, "JSON files (*.json);;All files (*)"
        )
        if path:
            edit.setText(path)

    def _prefill_from_env(self) -> None:
        try:
            core.load_env_file(getattr(core, "DEFAULT_ENV_FILE", ".env"))
        except Exception:
            pass
        self.client_id_edit.setText(os.getenv("YTMUSICAPI_CLIENT_ID", ""))
        self.client_secret_edit.setText(os.getenv("YTMUSICAPI_CLIENT_SECRET", ""))

    def current_credentials(self) -> tuple[str, str]:
        return self.client_id_edit.text().strip(), self.client_secret_edit.text().strip()

    def show_credentials_needed(self) -> None:
        self.overall_label.setText("Please enter your Google OAuth client ID and secret first (above).")

    def _save_env(self) -> None:
        client_id, client_secret = self.current_credentials()
        if not client_id or not client_secret:
            self.save_env_status.setText("Enter both values first.")
            return
        try:
            _write_env_values(
                SCRIPT_DIR / getattr(core, "DEFAULT_ENV_FILE", ".env"),
                {"YTMUSICAPI_CLIENT_ID": client_id, "YTMUSICAPI_CLIENT_SECRET": client_secret},
            )
            os.environ["YTMUSICAPI_CLIENT_ID"] = client_id
            os.environ["YTMUSICAPI_CLIENT_SECRET"] = client_secret
            self.save_env_status.setText("Saved to .env.")
        except Exception as exc:  # noqa: BLE001
            self.save_env_status.setText(f"Could not save: {friendly_error(exc)}")

    def _toggle_advanced(self, checked: bool) -> None:
        self.browser_box.setVisible(checked)
        self.source_section.box.setVisible(not checked)
        self.target_section.box.setVisible(not checked)
        self.creds_box.setVisible(not checked)
        self.refresh_overall()

    def initializePage(self) -> None:
        if self.tw.demo_mode:
            return
        self._prefill_from_env()
        self.source_section.refresh_status()
        self.target_section.refresh_status()

    def refresh_overall(self) -> None:
        if self.tw.demo_mode:
            return
        if self.advanced_check.isChecked():
            self.verify_btn.setEnabled(True)
            self.overall_label.setText("Point the app at both browser-header files, then verify.")
            return
        both_ready = self.source_section.ready and self.target_section.ready
        self.verify_btn.setEnabled(both_ready)
        if both_ready:
            self.overall_label.setText("Both accounts are signed in. Click “Verify accounts & continue”.")
        else:
            self.overall_label.setText("Sign in to both accounts to continue.")

    def _collect_config(self) -> dict[str, Any]:
        if self.advanced_check.isChecked():
            return {
                "auth_mode": "browser",
                "source_auth": self.browser_source_edit.text().strip(),
                "target_auth": self.browser_target_edit.text().strip(),
                "client_id": None,
                "client_secret": None,
                "keyring_service": KEYRING_SERVICE,
                "source_keyring_account": "source",
                "target_keyring_account": "target",
            }
        client_id, client_secret = self.current_credentials()
        return {
            "auth_mode": "oauth",
            "source_auth": self.source_section.token_path,
            "target_auth": self.target_section.token_path,
            "client_id": client_id or None,
            "client_secret": client_secret or None,
            "keyring_service": KEYRING_SERVICE,
            "source_keyring_account": "source",
            "target_keyring_account": "target",
        }

    def _verify(self) -> None:
        cfg = self._collect_config()
        if not cfg["source_auth"] or not cfg["target_auth"]:
            self.overall_label.setText("Please provide both account files.")
            return
        if cfg["auth_mode"] == "oauth" and (not cfg["client_id"] or not cfg["client_secret"]):
            self.show_credentials_needed()
            return
        self.verify_btn.setEnabled(False)
        self.overall_label.setText("Verifying access to both accounts…")
        self.tw.config.update(cfg)

        worker = Worker(_connect_worker, cfg)
        worker.logger = self.tw.logger
        worker.signals.log.connect(self.overall_label.setText)
        worker.signals.error.connect(self._on_verify_error)
        worker.signals.finished.connect(self._on_verify_finished)
        self.tw.pool.start(worker)

    def _on_verify_error(self, message: str) -> None:
        self.verify_btn.setEnabled(True)
        self.overall_label.setText(message)

    def _on_verify_finished(self, result: dict[str, Any]) -> None:
        self.tw.attach_session(result["stack"], result["source"], result["target"])
        self.verify_btn.setEnabled(True)
        self._connected = True
        self.tw.preview_loaded = False
        self.overall_label.setText("Connected. Both accounts verified. Click Next to continue.")
        self.completeChanged.emit()

    def cleanup_signins(self) -> None:
        if self.tw.demo_mode:
            return
        for section in (self.source_section, self.target_section):
            section.cancel()

    def isComplete(self) -> bool:
        return self._connected


def _demo_clients(scenario: str) -> tuple[Any, Any]:
    """Build (source, target) mock clients for a demo scenario."""
    cfg = mock_backend.make_config(scenario)
    source = mock_backend.MockYTMusic(cfg)
    target_cfg = mock_backend.MockConfig(
        payload={"tracks": []},  # target starts with no likes
        rate_limit_start=cfg.rate_limit_start,
        rate_limit_burst=cfg.rate_limit_burst,
        fail_track_indices=set(cfg.fail_track_indices),
        rate_network_start=cfg.rate_network_start,
        rate_network_burst=cfg.rate_network_burst,
        playlist_create_raise=cfg.playlist_create_raise,
        playlist_add_raise=cfg.playlist_add_raise,
    )
    target = mock_backend.MockYTMusic(target_cfg)
    return source, target


def _write_env_values(env_path: Path, values: dict[str, str]) -> None:
    """Create/update simple KEY=value lines in a local .env file."""
    env_path = Path(env_path)
    lines: list[str] = []
    if env_path.is_file():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    remaining = dict(values)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        replaced = False
        if "=" in stripped and not stripped.startswith("#"):
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                replaced = True
        if not replaced:
            out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _connect_worker(signals: WorkerSignals, cfg: dict[str, Any], logger: Any = None) -> dict[str, Any]:
    stack = ExitStack()
    try:
        signals.log.emit("Preparing authentication…")
        if cfg["auth_mode"] == "oauth":
            if _OAUTH_KEYRING is not None:
                source_path = stack.enter_context(
                    _OAUTH_KEYRING(cfg["source_auth"], cfg["keyring_service"], cfg["source_keyring_account"])
                )
                target_path = stack.enter_context(
                    _OAUTH_KEYRING(cfg["target_auth"], cfg["keyring_service"], cfg["target_keyring_account"])
                )
            else:
                core.require_file(cfg["source_auth"])
                core.require_file(cfg["target_auth"])
                source_path = cfg["source_auth"]
                target_path = cfg["target_auth"]
        else:
            core.require_file(cfg["source_auth"])
            core.require_file(cfg["target_auth"])
            source_path = cfg["source_auth"]
            target_path = cfg["target_auth"]

        signals.log.emit("Building account clients…")
        source = core.build_client(source_path, cfg["auth_mode"], cfg["client_id"], cfg["client_secret"])
        target = core.build_client(target_path, cfg["auth_mode"], cfg["client_id"], cfg["client_secret"])

        signals.log.emit("Verifying access to the source account…")
        call_api("get_liked_songs", lambda: source.get_liked_songs(limit=1), logger=logger)
    except BaseException:
        stack.close()
        raise
    return {"stack": stack, "source": source, "target": target}


# ---------------------------------------------------------------------------
# Step 2: Choose what to transfer
# ---------------------------------------------------------------------------
class OptionsPage(QWizardPage):
    def __init__(self, wizard: "TransferWizard") -> None:
        super().__init__()
        self.tw = wizard
        self.setTitle("Step 2 of 5 — Choose what to transfer")
        self.setSubTitle("Pick what should happen on the target account and how to filter the songs.")

        layout = QVBoxLayout(self)

        actions_box = QGroupBox("On the target account")
        actions_layout = QVBoxLayout(actions_box)
        self.like_check = QCheckBox("Like the songs on the target account (thumbs-up)")
        self.like_check.setChecked(True)
        self.playlist_check = QCheckBox("Also create a playlist containing the songs")
        actions_layout.addWidget(self.like_check)
        actions_layout.addWidget(self.playlist_check)

        playlist_row = QWidget()
        playlist_form = QFormLayout(playlist_row)
        self.playlist_title_edit = QLineEdit("Imported liked songs from account A")
        self.playlist_privacy_combo = QComboBox()
        self.playlist_privacy_combo.addItems(["PRIVATE", "UNLISTED", "PUBLIC"])
        playlist_form.addRow("Playlist name:", self.playlist_title_edit)
        playlist_form.addRow("Playlist privacy:", self.playlist_privacy_combo)
        playlist_row.setEnabled(False)
        actions_layout.addWidget(playlist_row)
        layout.addWidget(actions_box)

        filters_box = QGroupBox("Filters")
        filters_layout = QVBoxLayout(filters_box)
        self.skip_existing_check = QCheckBox("Skip songs already liked on the target account")
        self.skip_existing_check.setChecked(True)
        self.skip_unavailable_check = QCheckBox("Skip songs that YouTube marks unavailable in the source")
        self.skip_unavailable_check.setChecked(True)
        filters_layout.addWidget(self.skip_existing_check)
        filters_layout.addWidget(self.skip_unavailable_check)

        limit_row = QHBoxLayout()
        self.limit_check = QCheckBox("Limit to the first")
        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(1, 100000)
        self.limit_spin.setValue(50)
        self.limit_spin.setEnabled(False)
        limit_row.addWidget(self.limit_check)
        limit_row.addWidget(self.limit_spin)
        limit_row.addWidget(QLabel("songs (leave off to transfer all)"))
        limit_row.addStretch(1)
        filters_layout.addLayout(limit_row)
        layout.addWidget(filters_box)

        advanced_box = QGroupBox("Advanced (safe defaults)")
        advanced_form = QFormLayout(advanced_box)
        self.sleep_spin = QSpinBox()
        self.sleep_spin.setRange(0, 10000)
        self.sleep_spin.setValue(250)
        self.sleep_spin.setSuffix(" ms")
        self.chunk_spin = QSpinBox()
        self.chunk_spin.setRange(1, 100)
        self.chunk_spin.setValue(50)
        advanced_form.addRow("Pause between writes:", self.sleep_spin)
        advanced_form.addRow("Playlist batch size:", self.chunk_spin)
        layout.addWidget(advanced_box)
        layout.addStretch(1)

        self.playlist_check.toggled.connect(playlist_row.setEnabled)
        self.playlist_check.toggled.connect(lambda *_: self.completeChanged.emit())
        self.like_check.toggled.connect(lambda *_: self.completeChanged.emit())
        self.playlist_title_edit.textChanged.connect(lambda *_: self.completeChanged.emit())
        self.limit_check.toggled.connect(self.limit_spin.setEnabled)

    def isComplete(self) -> bool:
        if not self.like_check.isChecked() and not self.playlist_check.isChecked():
            return False
        if self.playlist_check.isChecked() and not self.playlist_title_edit.text().strip():
            return False
        return True

    def validatePage(self) -> bool:
        self.tw.config.update(
            {
                "do_like": self.like_check.isChecked(),
                "do_playlist": self.playlist_check.isChecked(),
                "playlist_title": self.playlist_title_edit.text().strip(),
                "playlist_privacy": self.playlist_privacy_combo.currentText(),
                "playlist_description": "Created by the Liked Songs Transfer app.",
                "skip_existing": self.skip_existing_check.isChecked(),
                "skip_unavailable": self.skip_unavailable_check.isChecked(),
                "limit": self.limit_spin.value() if self.limit_check.isChecked() else None,
                "sleep": self.sleep_spin.value() / 1000.0,
                "chunk_size": self.chunk_spin.value(),
                "fail_fast": False,
            }
        )
        # Force the preview to recompute with the latest options.
        self.tw.preview_loaded = False
        return True


# ---------------------------------------------------------------------------
# Step 3: Preview
# ---------------------------------------------------------------------------
class PreviewPage(QWizardPage):
    def __init__(self, wizard: "TransferWizard") -> None:
        super().__init__()
        self.tw = wizard
        self._ready = False
        self._empty = False
        self.setTitle("Step 3 of 5 — Preview")
        self.setSubTitle("Nothing is changed yet. This is exactly what will be transferred.")

        layout = QVBoxLayout(self)
        self.summary_label = QLabel("Loading your liked songs…")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        self.plan_label = QLabel("")
        self.plan_label.setWordWrap(True)
        layout.addWidget(self.plan_label)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Title", "Artist"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 380)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        layout.addWidget(self.table, 1)

    def initializePage(self) -> None:
        if self.tw.preview_loaded and self._ready:
            return
        self._ready = False
        self._empty = False
        self.completeChanged.emit()
        self.summary_label.setText("Reading your liked songs… this can take a moment for large libraries.")
        self.plan_label.setText("")
        self.table.setRowCount(0)

        worker = Worker(_preview_worker, self.tw.config, self.tw.source_client, self.tw.target_client)
        worker.logger = self.tw.logger
        worker.signals.log.connect(lambda msg: self.summary_label.setText(msg))
        worker.signals.error.connect(self._on_error)
        worker.signals.finished.connect(self._on_finished)
        self.tw.pool.start(worker)

    def _on_error(self, message: str) -> None:
        self.summary_label.setText(message)
        self.plan_label.setText("You can go Back to Step 1 to sign in again, or Step 2 to change filters.")

    def _on_finished(self, result: dict[str, Any]) -> None:
        self.tw.unique_tracks = result["unique"]
        self.tw.preview_stats = result
        self.tw.preview_loaded = True

        total = result["total_source"]
        unique = result["unique_count"]
        to_transfer = len(result["unique"])

        if total == 0:
            self._empty = True
            self.summary_label.setText(
                "No songs to transfer. Your source account has no liked songs to read.\n\n"
                "You can go Back to change the account or filters."
            )
            self.plan_label.setText("")
            self._ready = True
            self.completeChanged.emit()
            return

        if to_transfer == 0:
            self._empty = True
            parts = [f"Found {total} liked song(s) in the source ({unique} unique), but nothing is left to transfer"]
            reasons = []
            if result["unavailable_skipped"]:
                reasons.append(f"{result['unavailable_skipped']} unavailable")
            if result["already_liked"]:
                reasons.append(f"{result['already_liked']} already liked on the target")
            if result["dup_skipped"]:
                reasons.append(f"{result['dup_skipped']} duplicate(s)")
            if reasons:
                parts.append(" after skipping " + ", ".join(reasons))
            self.summary_label.setText("".join(parts) + ".\n\nGo Back to Step 2 to relax the filters.")
            self.plan_label.setText("")
            self._ready = True
            self.completeChanged.emit()
            return

        parts = [f"Found {total} liked songs in the source account ({unique} unique)."]
        if result["dup_skipped"]:
            parts.append(f"{result['dup_skipped']} duplicate(s) removed.")
        if result["unavailable_skipped"]:
            parts.append(f"{result['unavailable_skipped']} unavailable song(s) skipped.")
        if result["already_liked"]:
            parts.append(f"{result['already_liked']} already liked on the target and skipped.")
        parts.append(f"\n\n{to_transfer} song(s) will be transferred.")
        self.summary_label.setText(" ".join(parts))

        actions = []
        if self.tw.config.get("do_like"):
            actions.append("thumbs-up each song on the target account")
        if self.tw.config.get("do_playlist"):
            actions.append(
                f"create a {self.tw.config.get('playlist_privacy', 'PRIVATE').lower()} playlist "
                f"\u201c{self.tw.config.get('playlist_title')}\u201d"
            )
        if actions:
            self.plan_label.setText("On the next step the app will: " + " and ".join(actions) + ".")
        else:
            self.plan_label.setText("No write action selected.")

        preview_rows = result["unique"][:500]
        self.table.setRowCount(len(preview_rows))
        for row, track in enumerate(preview_rows):
            self.table.setItem(row, 0, QTableWidgetItem(track.title or track.video_id))
            self.table.setItem(row, 1, QTableWidgetItem(track.artists or ""))
        if to_transfer > len(preview_rows):
            self.summary_label.setText(self.summary_label.text() + f" (showing first {len(preview_rows)})")

        self._ready = True
        self.completeChanged.emit()

    def isComplete(self) -> bool:
        return self._ready and not self._empty and len(self.tw.unique_tracks) > 0


def _preview_worker(
    signals: WorkerSignals, cfg: dict[str, Any], source: Any, target: Any, logger: Any = None
) -> dict[str, Any]:
    on_retry = _make_on_retry(signals, logger)
    if logger is not None:
        logger.info("Preview: reading liked songs (limit=%s)", cfg.get("limit"))
    signals.log.emit("Reading liked songs from the source account…")
    payload = call_api(
        "get_liked_songs",
        lambda: source.get_liked_songs(limit=cfg.get("limit")),
        on_retry=on_retry,
        logger=logger,
    )
    tracks = core.extract_tracks(payload)
    total_source = len(tracks)
    unique, skipped = core.dedupe_tracks(tracks)
    dup_skipped = len(skipped)

    unavailable_skipped = 0
    if cfg.get("skip_unavailable"):
        kept = []
        for track in unique:
            if track.is_available is False:
                unavailable_skipped += 1
            else:
                kept.append(track)
        unique = kept

    already_liked = 0
    if cfg.get("skip_existing"):
        signals.log.emit("Reading existing likes on the target account…")
        target_likes = _target_liked_ids(target, on_retry=on_retry, logger=logger)
        before = len(unique)
        unique = [t for t in unique if t.video_id not in target_likes]
        already_liked = before - len(unique)

    if logger is not None:
        logger.info(
            "Preview done: total=%d unique=%d dup=%d unavailable=%d already_liked=%d to_transfer=%d",
            total_source, total_source - dup_skipped, dup_skipped, unavailable_skipped, already_liked, len(unique),
        )
    return {
        "total_source": total_source,
        "unique_count": total_source - dup_skipped,
        "dup_skipped": dup_skipped,
        "unavailable_skipped": unavailable_skipped,
        "already_liked": already_liked,
        "unique": unique,
    }


# ---------------------------------------------------------------------------
# Step 4: Transfer with live progress
# ---------------------------------------------------------------------------
class TransferPage(QWizardPage):
    def __init__(self, wizard: "TransferWizard") -> None:
        super().__init__()
        self.tw = wizard
        self._done = False
        self._started = False
        self.setTitle("Step 4 of 5 — Transfer")
        self.setSubTitle("This step writes to your target account. It only starts when you click Start.")

        layout = QVBoxLayout(self)
        self.banner = QLabel("Ready when you are. Click \u201cStart transfer\u201d to begin.")
        self.banner.setWordWrap(True)
        layout.addWidget(self.banner)

        self.start_btn = QPushButton("Start transfer")
        layout.addWidget(self.start_btn)

        self.progress = QProgressBar()
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        self.counts_label = QLabel("")
        layout.addWidget(self.counts_label)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        layout.addWidget(self.log_view, 1)

        self.start_btn.clicked.connect(self._start)

    def initializePage(self) -> None:
        self._done = False
        self._started = False
        self.progress.setValue(0)
        self.log_view.clear()
        self.counts_label.setText("")
        self.start_btn.setEnabled(True)
        count = len(self.tw.unique_tracks)
        self.banner.setText(
            f"About to transfer {count} song(s) to the target account. "
            "Click \u201cStart transfer\u201d to begin. You can safely close the window to abort before starting."
        )
        self.completeChanged.emit()

    def _start(self) -> None:
        if self._started:
            return
        self._started = True
        self.tw.cancel_event.clear()
        self.tw.transfer_running = True
        self.start_btn.setEnabled(False)
        self.banner.setText("Transferring… please keep this window open until it finishes.")
        back = self.wizard().button(QWizard.BackButton)
        if back:
            back.setEnabled(False)

        report = core.TransferReport(
            started_at=core.utc_now(),
            source_auth="keyring" if self.tw.config.get("auth_mode") == "oauth" else self.tw.config.get("source_auth", "mock"),
            target_auth="keyring" if self.tw.config.get("auth_mode") == "oauth" else self.tw.config.get("target_auth", "mock"),
            limit=self.tw.config.get("limit"),
            dry_run=False,
            skipped=[],
            failed=[],
        )
        report.total_source_tracks = self.tw.preview_stats.get("total_source", 0)
        report.unique_video_ids = self.tw.preview_stats.get("unique_count", 0)
        report.already_liked_on_target = self.tw.preview_stats.get("already_liked", 0)

        worker = Worker(
            _transfer_worker,
            self.tw.config,
            self.tw.target_client,
            self.tw.unique_tracks,
            report,
            self.tw.cancel_event,
        )
        worker.logger = self.tw.logger
        worker.signals.log.connect(self._append_log)
        worker.signals.counts.connect(self.counts_label.setText)
        worker.signals.progress.connect(self._on_progress)
        worker.signals.error.connect(self._on_error)
        worker.signals.finished.connect(self._on_finished)
        self.tw.pool.start(worker)

    def _append_log(self, message: str) -> None:
        self.log_view.appendPlainText(message)

    def _on_progress(self, done: int, total: int) -> None:
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(min(done, max(total, 1)))

    def _on_error(self, message: str) -> None:
        self._append_log(f"Problem: {message}")
        self.banner.setText("The transfer stopped early. See the log below. You can close the window and try again.")
        self.start_btn.setEnabled(True)
        self._started = False
        self.tw.transfer_running = False

    def _on_finished(self, report: Any) -> None:
        self.tw.final_report = report
        self.tw.transfer_running = False
        self._done = True
        if getattr(report, "cancelled", False):
            self.banner.setText("Transfer cancelled. Click Next to see what was done.")
        else:
            self.banner.setText("Transfer complete. Click Next to see the summary.")
        self.completeChanged.emit()

    def isComplete(self) -> bool:
        return self._done


def _transfer_worker(
    signals: WorkerSignals,
    cfg: dict[str, Any],
    target: Any,
    unique_tracks: list,
    report: Any,
    cancel_event: Optional[threading.Event] = None,
    logger: Any = None,
) -> Any:
    on_retry = _make_on_retry(signals, logger)
    do_like = bool(cfg.get("do_like"))
    do_playlist = bool(cfg.get("do_playlist"))
    n_tracks = len(unique_tracks)
    chunk_size = max(1, int(cfg.get("chunk_size", 50)))
    video_ids = [t.video_id for t in unique_tracks]
    n_batches = ((len(video_ids) + chunk_size - 1) // chunk_size) if (do_playlist and video_ids) else 0
    total = max((n_tracks if do_like else 0) + n_batches, 1)
    sleep_seconds = float(cfg.get("sleep", 0.25) or 0)

    state = {"done": 0, "ok": 0, "failed": 0}

    def advance() -> None:
        state["done"] = min(state["done"] + 1, total)
        signals.progress.emit(state["done"], total)

    def emit_counts(phase: str) -> None:
        signals.counts.emit(
            f"{phase} — liked OK: {state['ok']}, failed: {state['failed']}"
        )

    if logger is not None:
        logger.info("Transfer start: like=%s playlist=%s tracks=%d batches=%d", do_like, do_playlist, n_tracks, n_batches)

    signals.progress.emit(0, total)
    cancelled = False

    if do_like:
        signals.log.emit(f"Liking {n_tracks} song(s) on the target account…")
        for track in unique_tracks:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                signals.log.emit("Cancelling…")
                break
            try:
                call_api(
                    "rate_song",
                    lambda t=track: target.rate_song(t.video_id, LikeStatus.LIKE),
                    on_retry=on_retry,
                    logger=logger,
                )
                report.liked_on_target += 1
                state["ok"] += 1
                signals.log.emit(f"Liked: {track.title or track.video_id}")
            except Exception as exc:  # noqa: BLE001 - record and continue like the CLI.
                report.failed = report.failed or []
                report.failed.append({"action": "like", "track": asdict(track), "error": friendly_error(exc)})
                state["failed"] += 1
                if logger is not None:
                    logger.error("Failed to like %s", track.video_id, exc_info=True)
                signals.log.emit(f"Could not like {track.title or track.video_id}: {friendly_error(exc)}")
                if cfg.get("fail_fast"):
                    advance()
                    raise
            advance()
            emit_counts("Liking")
            if sleep_seconds > 0 and not cancelled:
                _interruptible_sleep(sleep_seconds, cancel_event)

    if do_playlist and not cancelled:
        signals.log.emit("Creating the playlist on the target account…")
        emit_counts("Creating playlist…")
        _create_playlist(signals, cfg, target, video_ids, report, on_retry=on_retry, logger=logger, advance=advance)

    report.finished_at = core.utc_now()
    report.cancelled = cancelled  # type: ignore[attr-defined]

    if not cancelled:
        signals.progress.emit(total, total)

    report_path = str(SCRIPT_DIR / getattr(core, "DEFAULT_REPORT", "transfer_report.json"))
    try:
        core.write_report(report_path, report)
        report.report_path = report_path  # type: ignore[attr-defined]
        signals.log.emit(f"Wrote report: {report_path}")
        if logger is not None:
            logger.info("Wrote report %s", report_path)
    except Exception:  # noqa: BLE001 - never crash on report writing.
        if logger is not None:
            logger.error("Failed to write report", exc_info=True)

    if cancelled:
        signals.log.emit(f"Cancelled — {report.liked_on_target} done.")
        emit_counts("Cancelled")
    else:
        emit_counts("Finished")
    return report


def _create_playlist(
    signals: WorkerSignals,
    cfg: dict[str, Any],
    target: Any,
    video_ids: list,
    report: Any,
    on_retry: Any = None,
    logger: Any = None,
    advance: Optional[Callable[[], None]] = None,
) -> None:
    if not video_ids:
        return
    chunk_size = max(1, int(cfg.get("chunk_size", 50)))
    chunks = [video_ids[i : i + chunk_size] for i in range(0, len(video_ids), chunk_size)]
    try:
        playlist_id = call_api(
            "create_playlist",
            lambda: target.create_playlist(
                title=cfg["playlist_title"],
                description=cfg.get("playlist_description", ""),
                privacy_status=cfg.get("playlist_privacy", "PRIVATE"),
                video_ids=chunks[0],
            ),
            on_retry=on_retry,
            logger=logger,
        )
        if not isinstance(playlist_id, str):
            raise RuntimeError(f"Unexpected create_playlist response: {playlist_id!r}")
        report.playlist_id = playlist_id
        report.playlist_added += len(chunks[0])
        signals.log.emit(f"Created playlist with {len(chunks[0])} song(s).")
    except Exception as exc:  # noqa: BLE001
        report.failed = report.failed or []
        report.failed.append({"action": "create_playlist", "title": cfg.get("playlist_title"), "error": friendly_error(exc)})
        if logger is not None:
            logger.error("create_playlist failed", exc_info=True)
        signals.log.emit(f"Could not create the playlist: {friendly_error(exc)}")
        if advance is not None:
            advance()
        if cfg.get("fail_fast"):
            raise
        return
    if advance is not None:
        advance()

    for chunk in chunks[1:]:
        try:
            call_api(
                "add_playlist_items",
                lambda c=chunk: target.add_playlist_items(report.playlist_id, videoIds=c, duplicates=False),
                on_retry=on_retry,
                logger=logger,
            )
            report.playlist_added += len(chunk)
            signals.log.emit(f"Added {len(chunk)} more song(s) to the playlist.")
        except Exception as exc:  # noqa: BLE001
            report.failed = report.failed or []
            report.failed.append(
                {"action": "add_playlist_items", "playlist_id": report.playlist_id, "error": friendly_error(exc)}
            )
            if logger is not None:
                logger.error("add_playlist_items failed", exc_info=True)
            signals.log.emit(f"Could not add a batch to the playlist: {friendly_error(exc)}")
            if cfg.get("fail_fast"):
                if advance is not None:
                    advance()
                raise
        if advance is not None:
            advance()


# ---------------------------------------------------------------------------
# Step 5: Done
# ---------------------------------------------------------------------------
class DonePage(QWizardPage):
    def __init__(self, wizard: "TransferWizard") -> None:
        super().__init__()
        self.tw = wizard
        self.setTitle("Step 5 of 5 — Done")
        self.setSubTitle("Your transfer has finished.")
        self.setFinalPage(True)

        layout = QVBoxLayout(self)
        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        self.summary_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.summary_label)

        self.open_report_btn = QPushButton("Show report file")
        self.open_report_btn.clicked.connect(self._open_report)
        layout.addWidget(self.open_report_btn)
        layout.addStretch(1)

    def initializePage(self) -> None:
        report = self.tw.final_report
        if report is None:
            self.summary_label.setText("No transfer was completed.")
            return
        failed = len(report.failed or [])
        transferred = report.liked_on_target
        cancelled = getattr(report, "cancelled", False)

        lines = []
        if cancelled:
            lines.append("Transfer cancelled — here is what was done before stopping.")
        elif failed:
            lines.append(f"Partial success: {transferred} transferred, {failed} failed (see log/report).")
        else:
            lines.append("Transfer complete.")
        lines.append("")
        lines.append(f"• Songs liked on the target account: {transferred}")
        if report.already_liked_on_target:
            lines.append(f"• Skipped (already liked): {report.already_liked_on_target}")
        if getattr(report, "playlist_id", None):
            lines.append(f"• Playlist created: {report.playlist_id} ({report.playlist_added} songs)")
            lines.append(f"  Open it at: https://music.youtube.com/playlist?list={report.playlist_id}")
        if failed:
            lines.append(f"• Songs/steps that could not be completed: {failed} (details in the report)")
        else:
            lines.append("• No failures.")
        path = getattr(report, "report_path", None)
        if path:
            lines.append("")
            lines.append(f"A detailed report was saved to:\n{path}")
        log_path = getattr(self.tw, "log_path", None)
        if log_path:
            lines.append(f"A full technical log is at:\n{log_path}")
        self.summary_label.setText("\n".join(lines))
        self._report_path = path

    def _open_report(self) -> None:
        path = getattr(self, "_report_path", None)
        if not path:
            return
        import subprocess

        try:
            if sys.platform == "darwin":
                subprocess.run(["open", "-R", path], check=False)
            else:
                subprocess.run(["xdg-open", str(Path(path).parent)], check=False)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# The wizard shell
# ---------------------------------------------------------------------------
class TransferWizard(QWizard):
    def __init__(self, demo_mode: bool = False) -> None:
        super().__init__()
        self.demo_mode = demo_mode
        title = "YouTube Music — Liked Songs Transfer"
        if demo_mode:
            title += "  (DEMO MODE)"
        self.setWindowTitle(title)
        self.setWizardStyle(QWizard.ModernStyle)
        self.setOption(QWizard.NoBackButtonOnStartPage, True)
        self.setMinimumSize(720, 620)

        # Per-run logger: friendly messages go to the UI, full detail to this file.
        try:
            self.logger, self.log_path = setup_run_logger()
        except Exception:
            self.logger, self.log_path = None, None

        # Shared state across pages.
        self.pool = QThreadPool.globalInstance()
        self.config: dict[str, Any] = {}
        self.exit_stack: Optional[ExitStack] = None
        self.source_client: Any = None
        self.target_client: Any = None
        self.unique_tracks: list = []
        self.preview_stats: dict[str, Any] = {}
        self.preview_loaded = False
        self.final_report: Any = None
        self.scenario: Optional[str] = None
        self.cancel_event = threading.Event()
        self.transfer_running = False

        self.setPage(PAGE_AUTH, AuthPage(self))
        self.setPage(PAGE_OPTIONS, OptionsPage(self))
        self.setPage(PAGE_PREVIEW, PreviewPage(self))
        self.setPage(PAGE_TRANSFER, TransferPage(self))
        self.setPage(PAGE_DONE, DonePage(self))
        self.setStartId(PAGE_AUTH)

    def attach_session(self, stack: Any, source: Any, target: Any) -> None:
        # Replace any earlier session (e.g. user reconnected).
        if self.exit_stack is not None:
            try:
                self.exit_stack.close()
            except Exception:
                pass
        self.exit_stack = stack if isinstance(stack, ExitStack) else None
        self.source_client = source
        self.target_client = target

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override.
        # Stop any OAuth polling and an in-flight transfer promptly.
        self.cancel_event.set()
        try:
            auth_page = self.page(PAGE_AUTH)
            if isinstance(auth_page, AuthPage):
                auth_page.cleanup_signins()
        except Exception:
            pass

        # Give a running transfer a brief moment to notice the cancel token,
        # finalize a partial report and stop cleanly.
        if self.transfer_running:
            if self.logger is not None:
                try:
                    self.logger.info("Window closed mid-transfer; requesting cancellation.")
                except Exception:
                    pass
            deadline = time.time() + 3.0
            while self.transfer_running and time.time() < deadline:
                QApplication.processEvents()
                time.sleep(0.05)
            # Wait for the thread pool to drain any remaining work briefly.
            try:
                self.pool.waitForDone(2000)
            except Exception:
                pass

        if self.exit_stack is not None:
            try:
                self.exit_stack.close()  # Persists OAuth token to keyring and removes temp files.
            except Exception:
                pass
            self.exit_stack = None

        if self.logger is not None:
            try:
                self.logger.info("=== Run finished (window closed) ===")
            except Exception:
                pass
        super().closeEvent(event)


def build_wizard(demo_mode: Optional[bool] = None) -> TransferWizard:
    """Construct the wizard (kept separate so it can be built headlessly in tests)."""
    if demo_mode is None:
        demo_mode = is_demo_mode()
    return TransferWizard(demo_mode=demo_mode)


def main() -> int:
    demo = is_demo_mode()
    app = QApplication(sys.argv)
    app.setApplicationName("Liked Songs Transfer")
    # A slightly larger base font reads better on macOS Retina displays.
    font = app.font()
    if font.pointSize() > 0:
        font.setPointSize(font.pointSize() + 1)
        app.setFont(font)
    wizard = build_wizard(demo)
    wizard.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
