FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# psycopg2-binary trae sus propias libs; bcrypt/aiosmtplib/apscheduler son wheels.
# gcc solo por si alguna wheel sin binario para la plataforma.
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY app ./app
COPY scripts ./scripts
COPY tests ./tests
COPY alembic.ini ./
COPY .env.example ./

RUN pip install .

EXPOSE 8000

# Bootstrap de tablas (dev) + arranque del servidor con lifespan (scheduler).
CMD ["sh", "-c", "python scripts/init_db.py && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
