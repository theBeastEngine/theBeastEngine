FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    DB_PATH=/data/beast.db \
    GOLD_BACKUP_PATH=/data/pinnacle_reference.json \
    BEAST_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN useradd --create-home beast \
    && mkdir -p /data \
    && chown -R beast:beast /app /data
USER beast

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health',timeout=4)" || exit 1

# one worker only: the scanner runs inside the web process, so more workers would duplicate every alert
CMD ["sh", "-c", "uvicorn api.server:app --host 0.0.0.0 --port ${PORT} --proxy-headers"]
