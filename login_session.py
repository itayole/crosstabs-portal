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
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

import download_crosstabs as dl

# Connecting to Decipher is a precondition for the app, so a login can be started without a
# survey in mind. With no survey URL we have no origin to derive, hence a configurable
# default -- set DECIPHER_ORIGIN if the account lives on a different regional host.
DEFAULT_ORIGIN = os.environ.get("DECIPHER_ORIGIN", "https://emea.focusvision.com").rstrip("/")

# Where a connect-only login window lands. Override with DECIPHER_LOGIN_URL if the sign-in
# page ever moves.
LOGIN_URL = os.environ.get("DECIPHER_LOGIN_URL", f"{DEFAULT_ORIGIN}/login")

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
    def __init__(self, survey_url: str = None, after_login=None, on_failure=None):
        self.survey_url = survey_url or None
        self.list_url = None
        # "survey": opened on a specific crosstabs page, so the crosstab list is a definitive
        # logged-in signal and the download can follow immediately.
        # "portal": just connecting, with no survey in mind -- weaker signal, so readiness has
        # to hold across consecutive polls before we trust it.
        self.mode = "survey" if survey_url else "portal"
        self._ready_streak = 0
        self.password = secrets.token_urlsafe(16)[:VNC_PASSWORD_LEN]
        # starting | awaiting_login | in_progress | ready | downloading | finished | closed
        self.state = "starting"
        self.error = None
        self.saved = False
        # The page must not point noVNC at the relay until x11vnc is actually listening:
        # connecting earlier fails outright, and noVNC does not retry by itself.
        self.vnc_ready = False
        self.target = None          # the URL the window opens on; surfaced for support
        self.created_at = time.time()

        # after_login(page, list_url) runs the download in this session's own browser, in
        # this thread. on_failure(reason) fires if we never get that far.
        self._after_login = after_login
        self._on_failure = on_failure
        self._handed_off = False

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
            "vnc_ready": self.vnc_ready,
            "target": self.target,
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
            if self.state not in ("finished", "closed"):
                self.state = "closed"
            # Nobody ever took over the browser, so whatever is waiting on the run has to be
            # told -- otherwise a timed-out or cancelled login leaves a job running forever.
            if not self._handed_off and self._on_failure is not None:
                try:
                    self._on_failure(self.error or "חלון ההתחברות נסגר לפני שההתחברות הושלמה.")
                except Exception:                    # noqa: BLE001 - best effort
                    pass

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
        self.vnc_ready = True

    def _drive_browser(self):
        if self.survey_url:
            self.list_url = dl.survey_list_url(self.survey_url)
            target = self.list_url
        else:
            target = LOGIN_URL
        self.target = target

        env = {**os.environ, "DISPLAY": DISPLAY}
        with sync_playwright() as playwright:
            browser = dl.launch_chromium(
                playwright,
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
            # accept_downloads because this same context goes on to do the crosstab
            # downloads once login completes.
            context = browser.new_context(no_viewport=True, accept_downloads=True)
            page = context.new_page()
            try:
                page.goto(target, wait_until="domcontentloaded", timeout=60_000)
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
            if command == "finish" and self.state == "awaiting_login":
                # Confirming while the login form is still on screen would save nothing
                # useful, so say so and keep the window open rather than failing later.
                self.error = "עדיין מוצג מסך ההתחברות. השלימו את ההתחברות ואז לחצו שוב."
                continue

            # Only an explicit confirmation proceeds. Detection still labels the state for the
            # page, but never triggers on its own: the user is the authority on whether the
            # login worked, which is the one signal that can't be misread.
            if command == "finish":
                self.error = None
                self._save(context)
                self._hand_off(page)
                return

        self.error = "חלון ההתחברות פג. נסו שוב."

    def _hand_off(self, page):
        """Carry straight on into the download, in the browser the user just logged in with.

        Runs inline in this thread, which is what the sync Playwright API requires. Note this
        is deliberately outside the login deadline loop: once downloading starts, the login
        timeout must not apply or a long run would be torn down mid-flight.
        """
        # Nothing to continue into when the user was only connecting.
        if self._after_login is None or self.survey_url is None:
            return

        self._handed_off = True
        self.state = "downloading"
        try:
            self._after_login(page, self.list_url)
        except Exception as exc:                     # noqa: BLE001 - reported via the job
            self.error = str(exc)
        finally:
            self.state = "finished"

    def _detect(self, page) -> str:
        try:
            if page.locator('input[type="password"]').count() > 0:
                self._ready_streak = 0
                return "awaiting_login"

            if self.mode == "survey":
                # Definitive: the crosstab list is exactly what the download needs.
                if page.locator("text=New Crosstab").count() > 0:
                    return "ready"
                return "in_progress"

            # Connect mode has no such marker and must NOT guess. An earlier version treated
            # "no password field on our origin" as success, which fired on the site root
            # before anyone had signed in and saved an unauthenticated session -- every later
            # run then failed for no visible reason. The user confirms instead, via "finish".
        except Exception:                            # noqa: BLE001 - mid-navigation
            pass
        return "in_progress"

    def _save(self, context):
        dl.AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(dl.AUTH_FILE))
        self.saved = True

    def _stop_processes(self):
        self._kill_stray_browsers()
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
        # After our own Popen children are waited on, anything left is an orphan.
        self._reap_zombies()

    @staticmethod
    def _reap_zombies():
        """Reap orphaned browser helpers.

        Chromium shuts down correctly, but some of its helper processes outlive their parent
        briefly and get reparented to PID 1 -- which is this app inside the container. Python
        never calls wait() on them, so they sit as state=Z entries: no memory, but a set of
        PID-table slots per login that never goes away. `init: true` in the compose files
        handles this too; doing it here means it holds however the container is started.
        """
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
            except OSError:                          # no children, or unsupported (Windows)
                return
            if pid == 0:                             # children exist but none have exited
                return

    @staticmethod
    def _kill_stray_browsers():
        """Backstop for Chromium surviving browser.close().

        Observed in practice: after a cancelled login the session reports closed and Xvfb and
        x11vnc are gone, but the Chromium process tree is still alive -- so every cancelled or
        timed-out login would strand a browser, and they accumulate for the life of the
        container. Matching on our own --window-size argument keeps this to browsers this
        module launched. Reads /proc directly because the slim image has no ps/pkill.
        """
        proc_root = Path("/proc")
        if not proc_root.is_dir():                   # not Linux (local dev on Windows)
            return

        marker = f"--window-size={SCREEN_W},{SCREEN_H}"

        cmdlines, parents = {}, {}
        for entry in proc_root.glob("[0-9]*"):
            try:
                pid = int(entry.name)
                cmdlines[pid] = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                    "utf-8", "ignore"
                )
                stat = (entry / "stat").read_text()
                # "pid (comm) state ppid ..." -- comm can contain spaces and parens, so parse
                # after the final ')': fields are then state, ppid, ...
                parents[pid] = int(stat[stat.rindex(")") + 2:].split()[1])
            except (OSError, ValueError):            # process vanished mid-read
                continue

        children = {}
        for pid, ppid in parents.items():
            children.setdefault(ppid, []).append(pid)

        def subtree(pid, acc):
            for child in children.get(pid, []):
                subtree(child, acc)
            acc.append(pid)                          # children before their parent
            return acc

        # Only the main browser process carries --window-size; its renderers and crashpad
        # handler do not, and SIGKILLing the parent alone just orphans them. So take the whole
        # subtree. Our own process is an ancestor of the browser, never inside this subtree.
        for root in [pid for pid, cmd in cmdlines.items() if marker in cmd]:
            for pid in subtree(root, []):
                try:
                    os.kill(pid, signal.SIGKILL)
                except (OSError, ValueError):
                    pass


_current = None
_current_lock = threading.Lock()


def start(survey_url: str = None, after_login=None, on_failure=None) -> dict:
    """Open a login window. With a survey URL it lands on that survey's crosstabs page and
    the caller's after_login runs the download; without one it just connects."""
    global _current
    with _current_lock:
        if _current is not None and _current.active:
            raise LoginBusy("התחברות אחרת מתבצעת כרגע. המתינו לסיומה או בטלו אותה.")
        reason = preflight()
        if reason:
            raise LoginUnavailable(reason)
        if survey_url:
            dl.parse_survey_url(survey_url)   # fail fast, before spawning anything
        _current = _Session(survey_url, after_login=after_login, on_failure=on_failure)
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
