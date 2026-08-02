# Crosstabs — portal feature

Web version of the crosstab downloader: paste a survey URL, click Run, get a download link for
one combined Excel workbook (every custom crosstab, trimmed to its Percentages sheet, one sheet
each — same pipeline as the desktop scripts one level up in this project).

Meant to run as its own Docker container in Container Station, exposed at `/crosstabs/` the same
way `oe-decipher` is exposed.

## Why login isn't automated here

Decipher requires 2FA, so there's no way for a headless container to log in on its own. Instead,
this container reuses the same `auth_state.json` session file the desktop script
(`download_crosstabs.py` in the parent folder) already produces after a one-time manual login.

**Setup / refresh flow:**
1. On any machine with a browser (e.g. your desktop), run the desktop script's login step:
   `python download_crosstabs.py "<any survey url>" --headed` and log in when the window opens.
2. Upload the resulting `auth_state.json` on the app's own page — the panel at the top shows
   whether a session is loaded and takes the file directly. No NAS or File Station access needed.
3. The page then works until that Decipher session expires. When it does, the run fails, the
   stale file is discarded automatically, and the upload panel reappears — repeat steps 1–2.

There's no way around this manual step given 2FA.

**The session does not survive a restart.** It's held in the container's temp dir, so any restart
— including redeploying an unrelated portal app, since Container Station restarts the whole
stack — means uploading it again. That's the trade-off for running with no volumes, the same way
`dna-charts` and `spss-claude` do. If the re-uploading becomes annoying, the alternative is a
one-file read-only bind mount at `AUTH_STATE_PATH`; nothing in the code needs to change for that.

Note the session file is accepted over the portal, which has no authentication in front of it.
That's the same exposure as every other app on the portal, but it's a live cookie, so it's worth
knowing.

## Storage

Nothing persists. Each job gets its own temp directory (so two people running the same survey
can't overwrite each other — `combine_crosstabs` rewrites the downloaded `.xlsx` files in place),
and that directory is deleted an hour after the job finishes, along with its download link.

## Running it

```
docker compose up -d --build
```

This builds the image and starts the container on port 5000. There are no volumes — upload
`auth_state.json` on the page once it's up.

Without compose:

```
docker build -t crosstabs-portal .
docker run -d -p 5000:5000 --name crosstabs ghcr.io/itayole/crosstabs-portal:latest
```

## Wiring it into the portal at /crosstabs/

Already wired in the `portal` repo (`../portal/`), following the same pattern as `oe-decipher`:
the gateway **strips** the `/crosstabs` prefix (`proxy_pass http://crosstabs:5000/;` — the
trailing slash does the stripping), so the container serves from `/` and `URL_PREFIX` stays unset.

Because the prefix is stripped, every URL the page uses is **relative** (`static/style.css`,
`api/run`, `api/status/<id>`, `download/<id>`) — root-absolute paths like `/api/run` would escape
the sub-path and hit the portal homepage instead. Keep them relative when editing
`templates/index.html` or the `download_url` in `app.py`. The gateway also 301s bare `/crosstabs`
to `/crosstabs/`, since relative URLs only resolve correctly with the trailing slash.

`URL_PREFIX=/crosstabs` still exists as an escape hatch if the app is ever put behind a proxy
that *keeps* the prefix, but it isn't used in the portal setup.

### auth_state.json on QNAP

Nothing to set up — the portal service declares no volumes. After the stack starts, open
`/crosstabs/` and upload `auth_state.json` on the page.

## Files

- `app.py` — Flask app: the web UI route, job-runner endpoints, and download endpoint.
- `download_crosstabs.py` / `combine_crosstabs.py` — same pipeline logic as the desktop scripts,
  adapted to run headless-only with configurable paths (env vars `AUTH_STATE_PATH`,
  `DOWNLOADS_DIR`).
- `templates/index.html`, `static/style.css` — the page itself (plain HTML/JS, no build step).
