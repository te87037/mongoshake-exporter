# 使用輕量級的 Python 映像作為基礎
FROM python:3.11-slim

# 設定工作目錄
WORKDIR /app

# 將 Exporter 腳本複製到容器內
COPY mongoshake_exporter.py /app/

# 安裝依賴項
RUN pip install --no-cache-dir prometheus_client requests

# 修正處：使用 key=value 格式
ENV EXPORTER_PORT=9900

# 暴露 Exporter 埠
EXPOSE ${EXPORTER_PORT}

# 設定容器啟動時執行的指令
CMD ["python", "mongoshake_exporter.py"]
