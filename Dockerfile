FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY autoapply/ ./autoapply/
COPY dashboard/ ./dashboard/

ENV PYTHONUNBUFFERED=1 \
    DB_PATH=/data/autoapply.sqlite \
    SCREENSHOT_DIR=/data/screenshots \
    PROFILE_PATH=/config/profile.json

CMD ["python", "-m", "autoapply", "--help"]
