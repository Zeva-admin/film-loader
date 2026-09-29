FROM python:3.11-slim
COPY --from=rclone/rclone:latest /usr/local/bin/rclone /usr/local/bin/rclone
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
COPY static static
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000}
