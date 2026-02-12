FROM python:3.14-slim
RUN apt-get update && apt-get install -y --no-install-recommends calibre && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir .
COPY processor.py .
RUN mkdir -p /data/epubs
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app /data
USER appuser
CMD ["python", "-u", "processor.py", "--daemon", "--kindle"]
