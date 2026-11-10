# Playwright + Chromium для OLX/Otodom (headless)
FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

WORKDIR /app
ENV PYTHONUNBUFFERED=1
ENV FINDHOME_DATA_DIR=/data
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data
VOLUME ["/data"]

CMD ["python", "main.py"]
