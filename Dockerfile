FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python3 -m pip install --no-cache-dir -r requirements.txt

COPY bot.py .
CMD ["xvfb-run", "-a", "python3", "bot.py"]
