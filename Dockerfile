FROM python:3.12-slim

# scipy/numpy/pandas wheels cover most needs, but keep build tools available
# in case a platform-specific wheel isn't published for this base image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x docker-entrypoint.sh

# SQLite DB, uploaded recordings, and collected static files all live here --
# mount a volume over /app/data (and /app/db.sqlite3's parent, i.e. /app) in
# docker-compose.yml so they survive container recreation.
RUN mkdir -p /app/data/uploads /app/staticfiles

EXPOSE 8000

ENTRYPOINT ["./docker-entrypoint.sh"]
