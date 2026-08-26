FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
COPY tests ./tests
COPY alembic.ini ./
COPY alembic ./alembic
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -e '.[dev]'
COPY . .
EXPOSE 8000
CMD ["uvicorn", "openclips.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
