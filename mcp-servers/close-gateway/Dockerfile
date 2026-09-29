FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir .
# Serve MCP over HTTP (Railway injects PORT; MCP_PORT overrides when set).
ENV MCP_TRANSPORT=sse
CMD ["python", "-m", "close_gateway.server"]
