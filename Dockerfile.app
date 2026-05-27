# ====================================================================
# OPTIMIZED APP DOCKERFILE - Multi-stage with Layer Caching
# ====================================================================
FROM procucev-base:local AS base

WORKDIR /app

# ====================================================================
# STAGE 1: Dependencies (rarely changes)
# ====================================================================
FROM procucev-base:local AS dependencies

# Copy only dependency files first (better caching)
COPY requirements.txt ./
RUN pip list | grep -E "(gunicorn|fastapi)" || echo "Dependencies already in base"

# ====================================================================
# STAGE 2: Application Code (changes frequently)
# ====================================================================
FROM base AS application

WORKDIR /app

# Copy application code
COPY app/ ./app/
COPY templates/ ./templates/
COPY gunicorn_config.py ./
COPY docker_entrypoint_app.sh ./
COPY .env* ./

# Make entrypoint executable
RUN chmod +x docker_entrypoint_app.sh

# Create logs directory
RUN mkdir -p logs/app

# ====================================================================
# FINAL STAGE: Runtime
# ====================================================================
FROM application AS runtime

EXPOSE 8005

HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8005/health || exit 1

# Use entrypoint script with log rotation
CMD ["./docker_entrypoint_app.sh"]
