# The scanner and its dashboard in one container: the deployment the phased
# plan calls for at phase 1, on Railway or Render.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CS_DB_PATH=/data/cryptosignal.db \
    CS_API_HOST=0.0.0.0

WORKDIR /app

COPY pyproject.toml README.md ./
COPY cryptosignal ./cryptosignal
RUN pip install --no-cache-dir ".[api]"

# SQLite needs a writable path that survives a redeploy; mount a volume here.
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

CMD ["sh", "-c", "cryptosignal serve --scan --host 0.0.0.0 --port ${PORT:-8000}"]
