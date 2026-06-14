FROM python:3.11-slim

# Cài ffmpeg ở cấp hệ thống (cần quyền root, Docker có sẵn)
RUN apt-get update && apt-get install -y ffmpeg nodejs && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY webapp/ ./webapp/

ENV PYTHONUTF8=1

CMD gunicorn --chdir webapp app:app --bind 0.0.0.0:$PORT --timeout 300 --workers 1
