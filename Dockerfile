FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MUSE2API_HOST=0.0.0.0 \
    MUSE2API_PORT=8130

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY muse2api/ ./muse2api/

# Run unprivileged: the cache/state directories are the only writable paths.
RUN useradd --create-home --uid 10001 muse \
    && mkdir -p /app/.muse-state /app/data/media \
    && chown -R muse:muse /app
USER muse

EXPOSE 8130

ENTRYPOINT ["python", "-m", "muse2api"]
CMD ["serve"]
