# Railway забороняє VOLUME (навіть у батьківському образі) — збираємо Playwright самі
FROM python:3.12-bookworm

WORKDIR /app
ENV PYTHONUNBUFFERED=1
ENV FINDHOME_DATA_DIR=/data
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY . .
RUN mkdir -p /data

CMD ["python", "main.py"]
