FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr poppler-utils && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV STAGE_DELAY=0.7
# one worker on purpose: live run state is kept in memory and polled by the UI
CMD gunicorn -w 1 --threads 8 --timeout 120 -b 0.0.0.0:${PORT:-8000} app:app
