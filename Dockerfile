FROM python:3.12-slim

# System-Dependencies für Playwright/Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Chromium runtime libs
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libdbus-1-3 libexpat1 libxcb1 libxkbcommon0 \
    libx11-6 libxcomposite1 libxdamage1 libxext6 libxfixes3 \
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    libatspi2.0-0 libxshmfence1 \
    # Font support
    fonts-liberation fonts-noto-color-emoji \
    # Misc
    wget ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright Chromium installieren
RUN python -m playwright install chromium

COPY . .

ENV DASHBOARD_HOST=0.0.0.0
ENV DASHBOARD_PORT=5000
ENV DATABASE_PATH=/data/ebay_monitor.db

VOLUME ["/data"]

EXPOSE 5000

CMD ["python", "dashboard.py"]
