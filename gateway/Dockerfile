FROM python:3.12-slim

WORKDIR /app

# 依赖单独一层，便于缓存。yt-dlp + ffmpeg 留到 M3 再加。
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
