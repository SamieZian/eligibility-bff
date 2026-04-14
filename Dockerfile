FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_VERSION=1.8.3 \
    POETRY_VIRTUALENVS_CREATE=false \
    POETRY_NO_INTERACTION=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential libpq-dev curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir poetry==$POETRY_VERSION

COPY libs/python-common /app/libs/python-common
COPY pyproject.toml poetry.lock ./
RUN poetry install --no-root --only main

COPY app /app/app
WORKDIR /app
ENV PYTHONPATH=/app:/app/libs/python-common/src

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=20 CMD curl -fsS http://localhost:8000/livez || exit 1
CMD ["python", "-m", "app.main"]
