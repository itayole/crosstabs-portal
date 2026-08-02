FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY . .

# Both paths are container-local scratch -- no volume. The session file is uploaded through
# the web UI and job output is handed straight back to the user, so nothing here needs to
# outlive the container. Restarting means re-uploading auth_state.json (see README.md).
ENV AUTH_STATE_PATH=/tmp/crosstabs/auth_state.json
ENV DOWNLOADS_DIR=/tmp/crosstabs/jobs

EXPOSE 5000

CMD ["python", "app.py"]
