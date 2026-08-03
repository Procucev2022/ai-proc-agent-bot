# ====================================================================
# UNIFIED DOCKERFILE - Azure Container Apps Production
# ====================================================================
FROM python:3.11-slim AS base

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Pre-download sentence transformer model so it's cached in the image
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# Copy application code and entrypoint scripts
COPY app/ ./app/
COPY templates/ ./templates/
COPY gunicorn_config.py ./
COPY docker_entrypoint_app.sh ./
COPY docker_entrypoint_celery.sh ./

# Make entrypoint scripts executable
RUN chmod +x docker_entrypoint_app.sh docker_entrypoint_celery.sh

# Create log directories
RUN mkdir -p logs/app logs/celery_worker

EXPOSE 8005

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8005/health || exit 1

CMD ["./docker_entrypoint_app.sh"]
