FROM python:3.11-slim

# Install Chromium and ChromeDriver (matched versions)
RUN apt-get update && apt-get install -y \
    chromium \
    chromium-driver \
    libnss3 \
    libxss1 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libgtk-3-0 \
    libasound2 \
    fonts-liberation \
    --no-install-recommends \
 && rm -rf /var/lib/apt/lists/*

# Environment variables for Selenium
ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app.py .

# Cloud Run listens on 8080
EXPOSE 8080

CMD ["python", "app.py"]
