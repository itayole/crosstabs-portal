"""
Crosstab downloader — portal feature.

Web UI: paste a survey URL, click Run, get a download link for one combined
Excel workbook (every custom crosstab's Percentages sheet, one sheet each).

Runs the same download -> trim -> combine pipeline as the desktop scripts,
but headless-only: login can't be automated here (Decipher requires 2FA), so
it relies on a pre-existing auth_state.json session file (see README.md).
"""

import json
import os
import shutil
import socket
import tempfile
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file, send_from_directory, abort
from flask_sock import Sock

import download_crosstabs as dl
import combine_crosstabs as combine
import login_session as login

app = Flask(__name__)

# noVNC offers the "binary" subprotocol when opening the RFB WebSocket. RFC 6455 lets a client
# fail the connection if the server selects none of the subprotocols it offered, and browsers
# do exactly that -- noVNC reported "Failed to connect to server" while a hand-rolled client
# offering no subprotocol connected fine, which is what made this hard to spot. Echo it.
app.config["SOCK_SERVER_OPTIONS"] = {"subprotocols": ["binary"]}

sock = Sock(app)

# Debian's novnc package. Served through this app rather than a second exposed port so the
# whole feature stays on one origin and needs one proxy rule.
NOVNC_DIR = Path(os.environ.get("NOVNC_DIR", "/usr/share/novnc"))


class PrefixMiddleware:
    """If the reverse proxy forwards requests keeping the /crosstabs prefix (instead of
    stripping it), set URL_PREFIX=/crosstabs so Flask's routing/url_for still line up.
    Leave URL_PREFIX unset if the proxy already strips the prefix -- see README.md."""

    def __init__(self, wsgi_app, prefix=""):
        self.wsgi_app = wsgi_app
        self.prefix = prefix

    def __call__(self, environ, start_response):
        if self.prefix and environ["PATH_INFO"].startswith(self.prefix):
            environ["SCRIPT_NAME"] = self.prefix
            environ["PATH_INFO"] = environ["PATH_INFO"][len(self.prefix):]
        return self.wsgi_app(environ, start_response)


url_prefix = os.environ.get("URL_PREFIX", "").rstrip("/")
if url_prefix:
    app.wsgi_app = PrefixMiddleware(app.wsgi_app, prefix=url_prefix)

# Job output is scratch, not data: the user downloads the workbook and that's the end of it.
# So it lives in a temp dir inside the container with no volume behind it, the same way the
# other portal apps (dna-charts, spss-claude, pdf-editor) hand back a file and keep nothing.
# Each job gets its own subfolder -- two people running the same survey at once would
# otherwise share one folder, and combine_crosstabs rewrites those .xlsx files in place.
JOBS_ROOT = Path(os.environ.get("DOWNLOADS_DIR", Path(tempfile.gettempdir()) / "crosstabs-jobs"))
JOBS_ROOT.mkdir(parents=True, exist_ok=True)

JOB_TTL_SECONDS = 3600  # after this, a job's status stops resolving and its files are deleted

SESSION_MAX_BYTES = 2 * 1024 * 1024  # auth_state.json is a few KB; anything near this is wrong

_jobs = {}
_jobs_lock = threading.Lock()


def _new_job():
    job_id = uuid.uuid4().hex
    job_dir = JOBS_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    with _jobs_lock:
        _jobs[job_id] = {
            "status": "running",       # running | done | error
            "log": [],
            "created_at": time.time(),
            "job_dir": job_dir,
            "result_file": None,       # absolute path, once done
            "error": None,
        }
    return job_id


def _discard_job(job_id):
    """Drop a job that never started, and its directory with it."""
    if not job_id:                                   # connect-only logins have no job
        return
    with _jobs_lock:
        job = _jobs.pop(job_id, None)
    if job:
        shutil.rmtree(job["job_dir"], ignore_errors=True)


def _log(job_id, message):
    with _jobs_lock:
        _jobs[job_id]["log"].append(message)


def _finish(job_id, result_file=None, error=None):
    with _jobs_lock:
        job = _jobs[job_id]
        job["status"] = "error" if error else "done"
        job["error"] = error
        job["result_file"] = str(result_file) if result_file else None


def _run_job(job_id, survey_url):
    with _jobs_lock:
        job_dir = _jobs[job_id]["job_dir"]
    try:
        # root= keeps the survey-named folder (and so the combined workbook's filename)
        # while confining it to this job's own directory.
        output_dir = dl.run_download(survey_url, root=job_dir, progress=lambda m: _log(job_id, m))
        combined_path = combine.run_combine(output_dir, progress=lambda m: _log(job_id, m))
        _finish(job_id, result_file=combined_path)
    except dl.LoginRequired as exc:
        # The stored session is expired (or absent), so it's dead weight -- drop it and the
        # page will show the upload prompt again instead of letting the next run fail too.
        dl.AUTH_FILE.unlink(missing_ok=True)
        _log(job_id, str(exc))
        _finish(job_id, error=str(exc))
    except Exception as exc:
        _log(job_id, f"Error: {exc}")
        _finish(job_id, error=str(exc))


def _continue_after_login(job_id, page, list_url):
    """Run the download in the browser the user just logged in with.

    Called from the login session's own thread once login is detected, so the authenticated
    browser is reused rather than discarded and rebuilt from auth_state.json.
    """
    with _jobs_lock:
        job_dir = _jobs[job_id]["job_dir"]
    try:
        output_dir = dl.download_all(
            page, list_url, root=job_dir, progress=lambda m: _log(job_id, m)
        )
        combined_path = combine.run_combine(output_dir, progress=lambda m: _log(job_id, m))
        _finish(job_id, result_file=combined_path)
    except Exception as exc:                         # noqa: BLE001 - surfaced on the job
        _log(job_id, f"Error: {exc}")
        _finish(job_id, error=str(exc))


def _fail_job(job_id, reason):
    with _jobs_lock:
        already_done = _jobs.get(job_id, {}).get("status") != "running"
    if already_done:
        return
    _log(job_id, reason)
    _finish(job_id, error=reason)


def _prune_old_jobs():
    cutoff = time.time() - JOB_TTL_SECONDS
    with _jobs_lock:
        stale = [jid for jid, j in _jobs.items() if j["created_at"] < cutoff]
        dirs = [_jobs[jid]["job_dir"] for jid in stale]
        for jid in stale:
            del _jobs[jid]
    # Outside the lock: rmtree can be slow and nothing else references these paths now.
    for job_dir in dirs:
        shutil.rmtree(job_dir, ignore_errors=True)


def _session_present():
    return dl.AUTH_FILE.exists()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/session", methods=["GET"])
def api_session_status():
    return jsonify({"present": _session_present()})


@app.route("/api/session", methods=["POST"])
def api_session_upload():
    """Accept an auth_state.json produced by the desktop login step.

    The session file can't be baked into the image (it's a live cookie, and it expires) and
    there's no volume to drop it into, so it's uploaded here and kept in the container's temp
    dir -- meaning it has to be re-uploaded after a restart. Decipher's 2FA is why the
    container can't just log in itself. See README.md.
    """
    uploaded = request.files.get("session_file")
    if uploaded is None or not uploaded.filename:
        return jsonify({"error": "לא נבחר קובץ."}), 400

    raw = uploaded.read(SESSION_MAX_BYTES + 1)
    if len(raw) > SESSION_MAX_BYTES:
        return jsonify({"error": "הקובץ גדול מדי — auth_state.json אמור להיות כמה קילובייטים."}), 400

    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return jsonify({"error": "הקובץ אינו JSON תקין — ודאו שזה auth_state.json."}), 400

    # Playwright's storage_state always has these two keys. Checking them catches the
    # common mistake of uploading some other .json before it fails mid-run instead.
    if not isinstance(parsed, dict) or "cookies" not in parsed or "origins" not in parsed:
        return jsonify({
            "error": "הקובץ אינו נראה כמו auth_state.json (חסרים cookies/origins)."
        }), 400

    dl.AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-replace so a failed upload can't leave a half-written session behind.
    temp_path = dl.AUTH_FILE.with_suffix(".tmp")
    temp_path.write_bytes(raw)
    os.replace(temp_path, dl.AUTH_FILE)

    return jsonify({"present": True})


@app.route("/api/login/start", methods=["POST"])
def api_login_start():
    data = request.get_json(silent=True) or {}
    survey_url = (data.get("survey_url") or "").strip()
    # The survey URL is optional here: connecting to Decipher is a precondition for using the
    # app at all, so it has to be possible before choosing a survey. When one *is* supplied we
    # open directly on it and carry on into the download.
    if survey_url:
        try:
            dl.parse_survey_url(survey_url)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    if not NOVNC_DIR.is_dir():
        return jsonify({
            "error": "רכיב התצוגה (noVNC) אינו מותקן בסביבה זו — התחברות בחלון זמינה "
                     "רק בקונטיינר. העלו קובץ auth_state.json במקום.",
            "unavailable": True,
        }), 503

    _prune_old_jobs()
    job_id = _new_job() if survey_url else None

    try:
        result = login.start(
            survey_url or None,
            after_login=(
                (lambda page, list_url: _continue_after_login(job_id, page, list_url))
                if job_id else None
            ),
            on_failure=(lambda reason: _fail_job(job_id, reason)) if job_id else None,
        )
    except login.LoginUnavailable as exc:
        _discard_job(job_id)
        return jsonify({"error": str(exc), "unavailable": True}), 503
    except login.LoginBusy as exc:
        _discard_job(job_id)
        return jsonify({"error": str(exc)}), 409
    except ValueError as exc:
        _discard_job(job_id)
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:                         # noqa: BLE001 - Xvfb/x11vnc failures
        _discard_job(job_id)
        return jsonify({"error": f"לא ניתן לפתוח חלון התחברות: {exc}"}), 500

    # job_id lets the page poll the run's progress the same way a manual Run does.
    return jsonify({**result, "job_id": job_id})


@app.route("/api/login/status")
def api_login_status():
    return jsonify(login.status())


@app.route("/api/login/type", methods=["POST"])
def api_login_type():
    """Type text into the login window (passwords pasted from a password manager).

    Deliberately not logged anywhere: it goes straight to the browser's keyboard.
    """
    data = request.get_json(silent=True) or {}
    text = data.get("text") or ""
    if not text:
        return jsonify({"error": "לא נשלח טקסט."}), 400
    try:
        return jsonify(login.type_text(text))
    except login.LoginBusy as exc:
        return jsonify({"error": str(exc)}), 409


@app.route("/api/login/finish", methods=["POST"])
def api_login_finish():
    return jsonify(login.finish())


@app.route("/api/login/cancel", methods=["POST"])
def api_login_cancel():
    return jsonify(login.cancel())


def _serve_novnc(filename):
    if not NOVNC_DIR.is_dir():
        abort(404)
    return send_from_directory(NOVNC_DIR, filename)


# Two independent rules, deliberately NOT one rule with defaults={"filename": "vnc.html"}.
# That form makes Werkzeug canonicalise /vnc/vnc.html to /vnc/ with a 308 whose Location is
# root-absolute. Behind the portal gateway the prefix is stripped before Flask sees the
# request, so Flask emits "/vnc/" with no idea /crosstabs exists -- the browser follows it to
# the portal root, nginx hands back the homepage, and the login window fills with the portal
# instead of Decipher. Serving the file directly means no redirect to get mangled.
@app.route("/vnc/")
def novnc_index():
    return _serve_novnc("vnc.html")


@app.route("/vnc/<path:filename>")
def novnc_static(filename):
    return _serve_novnc(filename)


def _close_quietly(ws):
    try:
        ws.close()
    except Exception:                                # noqa: BLE001 - already gone
        pass


@sock.route("/vnc-ws")
def vnc_ws(ws):
    """Relay the browser's WebSocket to x11vnc's RFB port.

    RFB over WebSocket is a plain byte stream in both directions, so this just copies bytes.
    Refusing to connect unless a login is actually in progress is deliberate: outside that
    window there is nothing listening on VNC_PORT and nothing to reach.
    """
    # noVNC retries on its own, so this endpoint is hit repeatedly after a login ends.
    # Returning here would drop an already-upgraded socket, which the client reports as
    # "1006 / Invalid frame header" -- noise that looks like a protocol fault. Close cleanly.
    if not login.is_active():
        _close_quietly(ws)
        return

    try:
        upstream = socket.create_connection(("127.0.0.1", login.VNC_PORT), timeout=5)
    except OSError:
        _close_quietly(ws)
        return

    stop = threading.Event()

    def upstream_to_client():
        try:
            while not stop.is_set():
                chunk = upstream.recv(65536)
                if not chunk:
                    break
                ws.send(chunk)
        except Exception:                            # noqa: BLE001 - client vanished
            pass
        finally:
            stop.set()

    pump = threading.Thread(target=upstream_to_client, daemon=True)
    pump.start()

    try:
        while not stop.is_set():
            # timeout so a silent client can't pin this thread past the login window
            data = ws.receive(timeout=1)
            if data is None:
                continue
            upstream.sendall(data.encode() if isinstance(data, str) else data)
    except Exception:                                # noqa: BLE001 - normal on disconnect
        pass
    finally:
        stop.set()
        try:
            upstream.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        upstream.close()


@app.route("/api/run", methods=["POST"])
def api_run():
    _prune_old_jobs()
    data = request.get_json(silent=True) or {}
    survey_url = (data.get("survey_url") or "").strip()

    try:
        dl.parse_survey_url(survey_url)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    job_id = _new_job()
    thread = threading.Thread(target=_run_job, args=(job_id, survey_url), daemon=True)
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def api_status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            abort(404)
        return jsonify({
            "status": job["status"],
            "log": job["log"],
            "error": job["error"],
            # Relative on purpose: the page may be served at / (standalone) or at
            # /crosstabs/ behind the portal gateway, which strips the prefix.
            "download_url": f"download/{job_id}" if job["result_file"] else None,
        })


@app.route("/download/<job_id>")
def download(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or not job["result_file"]:
            abort(404)
        result_file = job["result_file"]
    return send_file(result_file, as_attachment=True)


if __name__ == "__main__":
    # threaded=True: the pipeline runs in a background thread per job, but the server
    # itself still needs to serve concurrent /api/status polling requests while that runs.
    app.run(host="0.0.0.0", port=5000, threaded=True)
