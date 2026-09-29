FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir .
# Railway injects PORT; default 8000 for local runs.
CMD ["sh", "-c", "uvicorn agent_service.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
