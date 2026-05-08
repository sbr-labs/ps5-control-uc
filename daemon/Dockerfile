FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libc6-dev libffi-dev libssl-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY daemon.py .

# /data is mounted from host with credentials.json
VOLUME /data

ENV PYTHONUNBUFFERED=1
CMD ["python", "daemon.py"]
