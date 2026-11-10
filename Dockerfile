# Playwright + Chromium для OLX/Otodom (headless)
FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

WORKDIR /app
ENV PYTHONUNBUFFERED=1
ENV FINDHOME_DATA_DIR=/data
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# /data — тримай через Railway Volume (Settings → mount /data), не VOLUME в Dockerfile
RUN mkdir -p /data

CMD ["python", "main.py"]
