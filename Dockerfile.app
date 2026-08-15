# ====================================================================
# APPLICATION IMAGE - Azure Container Apps Production
# ====================================================================
# One image serves the web app, the Celery worker and Celery beat. The only
# difference between the roles is the command:
#
#   web app      -> ./docker_entrypoint_app.sh (the CMD below)
#   celery       -> overridden by infra/modules/celery-worker.bicep on Azure,
#                   and by docker-compose.yml locally
#
# There used to be a separate Dockerfile.celery whose content was a strict
# subset of this one, which meant every deploy installed the same ~2GB of
# dependencies twice. It is gone; build this file and tag it for both.
#
# The dependency layers live in Dockerfile.base so they can be built once and
# reused. BASE_IMAGE defaults to the locally built tag used by
# procucev-agent.sh; CI passes the registry-hosted tag instead.
# ====================================================================
ARG BASE_IMAGE=procucev-base:local
FROM ${BASE_IMAGE}

WORKDIR /app

# Copy application code and entrypoint scripts
COPY app/ ./app/
COPY Setup/ ./Setup/
COPY templates/ ./templates/
COPY gunicorn_config.py ./
COPY docker_entrypoint_app.sh ./
COPY docker_entrypoint_celery.sh ./

# Convert line endings to Unix format and make scripts executable
RUN sed -i 's/\r$//' docker_entrypoint_app.sh docker_entrypoint_celery.sh && \
    chmod +x docker_entrypoint_app.sh docker_entrypoint_celery.sh

# Create log directories
RUN mkdir -p logs/app logs/celery_worker

EXPOSE 8005

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8005/health || exit 1

CMD ["./docker_entrypoint_app.sh"]
