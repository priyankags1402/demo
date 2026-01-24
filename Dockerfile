FROM python:3.11-slim

# -----------------------------
# Install Chromium + Chromedriver + required libs
# -----------------------------
RUN apt-get update && apt-get install -y \
    chromium \
    chromium-driver \
    fonts-liberation \
    libnss3 \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libgtk-3-0 \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcups2 \
    libdrm2 \
    libxshmfence1 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# -----------------------------
# Environment variables for Selenium
# -----------------------------
ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver
ENV PYTHONUNBUFFERED=1

# -----------------------------
# Create non-root user (Chrome stability)
# -----------------------------
RUN useradd -m appuser
USER appuser

# -----------------------------
# App setup
# -----------------------------
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# -----------------------------
# Cloud Run / Flask
# -----------------------------
EXPOSE 8080

CMD ["python", "app.py"]
