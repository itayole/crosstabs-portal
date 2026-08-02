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
import tempfile
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file, abort

import download_crosstabs as dl
import combine_crosstabs as combine

app = Flask(__name__)


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
