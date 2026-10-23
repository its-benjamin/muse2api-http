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

# Create state directory
RUN mkdir -p /app/.muse-state /app/data/media

EXPOSE 8130

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8130/healthz')" || exit 1

ENTRYPOINT ["python", "-m", "muse2api"]
CMD ["serve"]
