FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       tesseract-ocr \
       tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --retries 8 --timeout 120 -r requirements.txt

COPY app.py ./

EXPOSE 8189

CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8189"]
