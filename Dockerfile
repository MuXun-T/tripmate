FROM python:3.12.8-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.lock pyproject.toml ./
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.lock

COPY app ./app
COPY migrations ./migrations
COPY redis ./redis
RUN python -m pip install --no-cache-dir --no-deps . \
    && useradd --uid 10001 --create-home tripmate \
    && chown -R tripmate:tripmate /app

USER 10001:10001
EXPOSE 8000

CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
