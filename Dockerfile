# RetailPulse API image. This packages the FastAPI app only -- PostgreSQL runs as its own
# container/service (see docker-compose.yml), never bundled into the app image.
FROM python:3.12-slim

# libpq is needed by psycopg[binary] at runtime on slim images for some platforms; kept minimal.
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code and the two things it reads at runtime: db/ (schema/grants SQL) and the
# scripts used to initialize/seed the database. Deliberately NOT copied: tests/, data/raw (the
# sample dataset -- generate it inside the container instead), artifacts/ (forecast models are
# trained as a one-off job against the running database, not baked into the image), and .env
# (secrets never go in the image).
COPY app/ app/
COPY etl/ etl/
COPY db/ db/
COPY scripts/ scripts/
COPY evaluation/ evaluation/

# Non-root user: the image should not run as root.
RUN useradd --create-home --uid 1000 retailpulse \
    && mkdir -p /srv/artifacts/models /srv/data/raw /srv/data/rejected \
    && chown -R retailpulse:retailpulse /srv
USER retailpulse

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD \
    python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
