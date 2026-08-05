"""Interactive Decipher login: a headed Chromium running inside the container, shown to the
user over VNC so they can complete 2FA themselves.

Why it has to work this way: a popup window from the user's own browser would authenticate
*their* browser, storing cookies against Decipher's origin where neither this page's
JavaScript nor the server can read them. The container's Playwright browser would stay
logged out. So we invert it -- the user drives our browser, and the session lands exactly
where the download pipeline needs it. context.storage_state() then writes the same
auth_state.json the desktop login step used to produce.

Playwright's sync API is bound to the thread that created it, so one worker thread owns the
whole session (Xvfb -> x11vnc -> Chromium) and takes instructions through a queue. Only one
login runs at a time; there's a single virtual display and VNC port.
"""

import os
import queue
import secrets
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

import download_crosstabs as dl

DISPLAY = os.environ.get("LOGIN_DISPLAY", ":99")
VNC_PORT = int(os.environ.get("LOGIN_VNC_PORT", "5900"))
SCREEN_W, SCREEN_H = 1280, 860
SCREEN = f"{SCREEN_W}x{SCREEN_H}x24"

# Long enough to find a phone and type a 2FA code, short enough that a forgotten tab doesn't
# leave a live browser (and an open VNC port) sitting there indefinitely.
SESSION_TIMEOUT_SECONDS = int(os.environ.get("LOGIN_TIMEOUT_SECONDS", "420"))

# RFB caps passwords at 8 characters -- x11vnc silently truncates anything longer, so
# generating something longer would only give a false sense of strength.
VNC_PASSWORD_LEN = 8


class LoginBusy(Exception):
    """Another login is already in progress."""


class LoginUnavailable(Exception):
    """This environment can't run the streamed login (e.g. the local venv on Windows)."""


REQUIRED_BINARIES = ("Xvfb", "x11vnc")


def preflight() -> str | None:
    """Reason the streamed login can't run here, or None if it can.

    Checked before spawning anything: without this, a missing Xvfb surfaces as a bare
    "[WinError 2] The system cannot find the file specified" from a worker thread, which
    tells the user nothing. Only the Docker image carries these binaries.
    """
    missing = [name for name in REQUIRED_BINARIES if shutil.which(name) is None]
    if missing:
        return (
            "התחברות בחלון זמינה רק כשהשירות רץ בקונטיינר Docker "
            f"(חסר בסביבה זו: {', '.join(missing)}). "
            "העלו קובץ auth_state.json במקום."
        )
    return None


def _wait_for_x_socket(display: str, timeout: float = 15.0) -> None:
    # ":99" -> /tmp/.X11-unix/X99
    sock_path = Path("/tmp/.X11-unix") / ("X" + display.lstrip(":").split(".")[0])
    deadline = time.time() + timeout
    while time.time() < deadline:
        if sock_path.exists():
            return
        time.sleep(0.1)
    raise RuntimeError(f"Xvfb did not come up on {display}")


def _wait_for_port(port: int, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"VNC server did not start listening on port {port}")


class _Session:
    def __init__(self, survey_url: str):
        self.survey_url = survey_url
        self.password = secrets.token_urlsafe(16)[:VNC_PASSWORD_LEN]
        self.state = "starting"   # starting | awaiting_login | in_progress | ready | closed
        self.error = None
        self.saved = False
        self.created_at = time.time()

        self._commands = queue.Queue()
        self._closed = threading.Event()
        self._procs = []
        self._thread = threading.Thread(target=self._run, daemon=True)

    # ---- lifecycle -------------------------------------------------------------------

    def start(self):
        self._thread.start()

    @property
    def active(self) -> bool:
        return not self._closed.is_set()

    def request(self, command: str):
        if self.active:
            self._commands.put(command)

    def snapshot(self) -> dict:
        """Deliberately excludes the VNC password. Status is pollable by anyone who can reach
        the portal, so handing the password out here would defeat it -- it goes only to
        whoever started the login, in the /api/login/start response."""
        return {
            "active": self.active,
            "state": self.state,
            "saved": self.saved,
            "error": self.error,
            "width": SCREEN_W,
            "height": SCREEN_H,
            "seconds_left": max(0, int(self.created_at + SESSION_TIMEOUT_SECONDS - time.time())),
        }

    # ---- worker ----------------------------------------------------------------------

    def _run(self):
        try:
            self._start_display()
            self._drive_browser()
        except Exception as exc:                     # noqa: BLE001 - surfaced to the page
            self.error = str(exc)
            self.state = "closed"
        finally:
            self._stop_processes()
            self._closed.set()
            if self.state != "closed":
                self.state = "closed"

    def _start_display(self):
        self._procs.append(subprocess.Popen(
            ["Xvfb", DISPLAY, "-screen", "0", SCREEN, "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ))
        _wait_for_x_socket(DISPLAY)

        # -localhost: only the in-process WebSocket relay may connect, never the network
        # directly. The password is what actually gates the relay's clients.
        self._procs.append(subprocess.Popen(
            [
                "x11vnc", "-display", DISPLAY, "-rfbport", str(VNC_PORT),
                "-localhost", "-passwd", self.password,
                "-forever", "-shared", "-noxdamage", "-quiet",
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ))
        _wait_for_port(VNC_PORT)

    def _drive_browser(self):
        origin, survey_path = dl.parse_survey_url(self.survey_url)
        list_url = f"{origin}/apps/report/{survey_path}#!/"

        env = {**os.environ, "DISPLAY": DISPLAY}
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=False,
                env=env,
                args=[
                    "--window-position=0,0",
                    f"--window-size={SCREEN_W},{SCREEN_H}",
                    "--no-first-run",
                    "--disable-infobars",
                ],
            )
            # no_viewport so the page fills the window the user is actually looking at.
            context = browser.new_context(no_viewport=True)
            page = context.new_page()
            try:
                page.goto(list_url, wait_until="domcontentloaded", timeout=60_000)
                self._command_loop(page, context)
            finally:
                try:
                    browser.close()
                except Exception:                    # noqa: BLE001 - already tearing down
                    pass

    def _command_loop(self, page, context):
        deadline = self.created_at + SESSION_TIMEOUT_SECONDS
        while time.time() < deadline:
            try:
                command = self._commands.get(timeout=1.0)
            except queue.Empty:
                command = "poll"

            if command == "cancel":
                return

            self.state = self._detect(page)

            # Auto-save the moment the survey's crosstab list is reachable -- that is the
            # same check the download pipeline uses, so if it passes here the run will work.
            # "finish" lets the user force it if the heuristic misses.
            if self.state == "ready" or command == "finish":
                self._save(context)
                return

        self.error = "חלון ההתחברות פג. נסו שוב."

    @staticmethod
    def _detect(page) -> str:
        try:
            if page.locator('input[type="password"]').count() > 0:
                return "awaiting_login"
            if page.locator("text=New Crosstab").count() > 0:
                return "ready"
        except Exception:                            # noqa: BLE001 - mid-navigation
            pass
        return "in_progress"

    def _save(self, context):
        dl.AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(dl.AUTH_FILE))
        self.saved = True

    def _stop_processes(self):
        for proc in reversed(self._procs):
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:                        # noqa: BLE001 - best effort
                try:
                    proc.kill()
                except Exception:                    # noqa: BLE001
                    pass
        self._procs.clear()


_current = None
_current_lock = threading.Lock()


def start(survey_url: str) -> dict:
    global _current
    with _current_lock:
        if _current is not None and _current.active:
            raise LoginBusy("התחברות אחרת מתבצעת כרגע. המתינו לסיומה או בטלו אותה.")
        reason = preflight()
        if reason:
            raise LoginUnavailable(reason)
        dl.parse_survey_url(survey_url)   # fail fast on a bad URL, before spawning anything
        _current = _Session(survey_url)
        _current.start()
        # The one place the VNC password is disclosed: to the client that opened the login.
        return {**_current.snapshot(), "password": _current.password}


def status() -> dict:
    with _current_lock:
        if _current is None:
            return {"active": False, "state": "idle", "saved": False, "error": None}
        return _current.snapshot()


def finish() -> dict:
    with _current_lock:
        if _current is not None:
            _current.request("finish")
        return status_unlocked()


def cancel() -> dict:
    with _current_lock:
        if _current is not None:
            _current.request("cancel")
        return status_unlocked()


def status_unlocked() -> dict:
    if _current is None:
        return {"active": False, "state": "idle", "saved": False, "error": None}
    return _current.snapshot()


def is_active() -> bool:
    with _current_lock:
        return _current is not None and _current.active
