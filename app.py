"""
Crosstab downloader — portal feature.

Web UI: paste a survey URL, click Run, get a download link for one combined
Excel workbook (every custom crosstab's Percentages sheet, one sheet each).

Runs the same download -> trim -> combine pipeline as the desktop scripts,
but headless-only: login can't be automated here (Decipher requires 2FA), so
it relies on a pre-existing auth_state.json session file (see README.md).
"""

import os
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

DOWNLOADS_ROOT = Path(os.environ.get("DOWNLOADS_DIR", Path(__file__).resolve().parent / "downloads"))
DOWNLOADS_ROOT.mkdir(parents=True, exist_ok=True)

JOB_TTL_SECONDS = 3600  # stop reporting a finished job's status after this long

_jobs = {}
_jobs_lock = threading.Lock()


def _new_job():
    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {
            "status": "running",       # running | done | error
            "log": [],
            "created_at": time.time(),
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
    try:
        output_dir = dl.run_download(survey_url, progress=lambda m: _log(job_id, m))
        combined_path = combine.run_combine(output_dir, progress=lambda m: _log(job_id, m))
        _finish(job_id, result_file=combined_path)
    except dl.LoginRequired as exc:
        _log(job_id, str(exc))
        _finish(job_id, error=str(exc))
    except Exception as exc:
        _log(job_id, f"Error: {exc}")
        _finish(job_id, error=str(exc))


def _prune_old_jobs():
    cutoff = time.time() - JOB_TTL_SECONDS
    with _jobs_lock:
        stale = [jid for jid, j in _jobs.items() if j["created_at"] < cutoff]
        for jid in stale:
            del _jobs[jid]


@app.route("/")
def index():
    return render_template("index.html")


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
