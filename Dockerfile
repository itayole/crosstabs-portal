FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

# Interactive Decipher login (see login_session.py): the user drives a headed Chromium
# running here, streamed to their browser. Xvfb gives it a display, x11vnc exposes that
# display on localhost, and novnc supplies the client the page embeds. The WebSocket relay
# lives in the app itself, so websockify isn't needed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb x11vnc novnc \
    && rm -rf /var/lib/apt/lists/*

COPY . .

# Both paths are container-local scratch -- no volume. The session file is uploaded through
# the web UI and job output is handed straight back to the user, so nothing here needs to
# outlive the container. Restarting means re-uploading auth_state.json (see README.md).
ENV AUTH_STATE_PATH=/tmp/crosstabs/auth_state.json
ENV DOWNLOADS_DIR=/tmp/crosstabs/jobs

EXPOSE 5000

CMD ["python", "app.py"]
