FROM python:3.12-slim

WORKDIR /app

# 依赖单独一层，便于缓存。yt-dlp + ffmpeg 留到 M3 再加。
# PyPI 源默认走清华镜像（国内 VM 上 pypi.org 常卡到超时）。
# 海外构建可覆盖：docker build --build-arg PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
COPY requirements.txt .
RUN pip install --no-cache-dir --index-url ${PIP_INDEX_URL} -r requirements.txt

COPY app ./app

# Web 面板的 Vue 运行时：构建时从 npmmirror（国内快）拉到本地托管，运行时不依赖 CDN。
ARG VUE_URL=https://registry.npmmirror.com/vue/3.5.13/files/dist/vue.global.prod.js
RUN for i in 1 2 3 4 5; do \
      python -c "import urllib.request; urllib.request.urlretrieve('${VUE_URL}', 'app/static/vue.global.prod.js')" && break; \
      echo "retry vue download $i"; sleep 3; \
    done; \
    test -s app/static/vue.global.prod.js

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
