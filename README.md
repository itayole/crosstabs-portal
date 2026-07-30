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
2. Copy the resulting `auth_state.json` into this container's mounted `./data` folder (see
   docker-compose.yml below) — i.e. `./data/auth_state.json`.
3. The portal page will then work until that Decipher session expires, at which point it'll show
   a clear "session expired" error and you repeat steps 1–2.

There's no way around this manual refresh step given 2FA — but it should be infrequent (however
often Decipher expires your session).

## Running it

```
docker compose up -d --build
```

This builds the image, starts the container, maps port 5000, and mounts `./data` for
`auth_state.json` + downloaded files (so they survive container restarts).

Without compose:

```
docker build -t crosstabs-portal .
docker run -d -p 5000:5000 -v $(pwd)/data:/data --name crosstabs crosstabs-portal
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

The portal service bind-mounts `/share/Container/portal/crosstabs-data` to `/data` (a named volume
would be awkward — this file has to be replaced by hand via File Station every time the Decipher
session expires). Put the refreshed `auth_state.json` directly in that folder; `downloads/` is
created next to it and persists across restarts.

## Files

- `app.py` — Flask app: the web UI route, job-runner endpoints, and download endpoint.
- `download_crosstabs.py` / `combine_crosstabs.py` — same pipeline logic as the desktop scripts,
  adapted to run headless-only with configurable paths (env vars `AUTH_STATE_PATH`,
  `DOWNLOADS_DIR`).
- `templates/index.html`, `static/style.css` — the page itself (plain HTML/JS, no build step).
