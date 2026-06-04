FROM python:3.12-slim

WORKDIR /app

# 依赖单独一层，便于缓存。yt-dlp + ffmpeg 留到 M3 再加。
# PyPI 源默认走清华镜像（国内 VM 上 pypi.org 常卡到超时）。
# 海外构建可覆盖：docker build --build-arg PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
COPY requirements.txt .
RUN pip install --no-cache-dir --index-url ${PIP_INDEX_URL} -r requirements.txt

COPY app ./app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
