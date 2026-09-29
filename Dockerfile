FROM python:3.12-slim
COPY --from=rclone/rclone:latest /usr/local/bin/rclone /usr/local/bin/rclone
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
COPY static static
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000}
