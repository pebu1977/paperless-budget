FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DB_PATH=/data/budget.db HOME=/tmp
WORKDIR /srv

# LibreOffice (nur Writer) für die PDF-Erzeugung der Nebenkostenabrechnung
RUN apt-get update && apt-get install -y --no-install-recommends libreoffice-writer-nogui fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/index.html'); sys.exit(0)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
