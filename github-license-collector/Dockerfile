ARG PYTHON_BASE_IMAGE=python:3.12-slim-bookworm
FROM ${PYTHON_BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --requirement requirements.txt

COPY app ./app

RUN groupadd --system --gid 65532 collector \
    && useradd --system --uid 65532 --gid collector --home-dir /nonexistent collector

USER 65532:65532

ENTRYPOINT ["python", "-m", "app.main"]

