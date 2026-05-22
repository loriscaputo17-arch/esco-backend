FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system deps (curl per healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps (separato per cache)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY app ./app

# Railway sets PORT automatically — usalo se presente, default 8000
ENV PORT=8000
EXPOSE 8000

# Healthcheck interno (Railway lo usa pure)
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:${PORT}/health || exit 1

# Avvia uvicorn senza --reload (production)
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
