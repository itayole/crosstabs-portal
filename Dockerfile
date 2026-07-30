FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY . .

ENV AUTH_STATE_PATH=/data/auth_state.json
ENV DOWNLOADS_DIR=/data/downloads

EXPOSE 5000

CMD ["python", "app.py"]
