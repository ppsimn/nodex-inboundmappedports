FROM python:3.12-slim

# Install minimal tools and tini for proper signal/zombie handling
RUN apt-get update && apt-get install -y --no-install-recommends \
  ca-certificates tini \
  && rm -rf /var/lib/apt/lists/*

# Create a non-root user for running the application
RUN useradd -m -u 10001 app
WORKDIR /app

# Install Python dependencies
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy application source code
COPY src/ /app/src/

# Create config and data directories, set ownership to app user
RUN mkdir -p /app/config /app/data && chown -R app:app /app

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
  LOG_LEVEL=INFO \
  HEALTH_MAX_AGE=180 \
  CONFIG_FILE=/app/config/config.json \
  DATA_DIR=/app/data \
  DB_FILE=/app/data/traffic_state.db \
  ENABLE_FILE_LOG=0

# Healthcheck: .heartbeat file must be newer than HEALTH_MAX_AGE seconds
HEALTHCHECK --interval=30s --timeout=3s --retries=3 CMD \
  python -c "import os,sys,time; p=os.path.join(os.getenv('DATA_DIR','/app/data'),'.heartbeat'); mx=int(os.getenv('HEALTH_MAX_AGE','180')); sys.exit(0 if (os.path.exists(p) and (time.time()-os.path.getmtime(p) < mx)) else 1)"

# Run securely as the app user
USER app
ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["python","-m","src.main"]
