FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/usage.db \
    PORT=8787

WORKDIR /app
COPY app ./app

RUN mkdir -p /data && chown -R 10001:10001 /data

USER 10001:10001
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/api/health', timeout=3)"

CMD ["python", "-m", "app.main"]
