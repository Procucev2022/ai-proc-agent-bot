# ====================================================================
# MAIN APPLICATION - USES SHARED BASE IMAGE
# ====================================================================
FROM procucev-python-base:latest

WORKDIR /app

# Copy application code only
COPY . .

# Create logs directory
RUN mkdir -p logs

EXPOSE 8005

HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8005/health || exit 1

# Use gunicorn_config.py for configuration
CMD ["gunicorn", "app.main:app", "--config", "gunicorn_config.py"]