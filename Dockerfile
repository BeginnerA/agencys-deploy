FROM python:3.11-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目文件
COPY . .

# 创建必要的目录
RUN mkdir -p data/logs data/runtime data/export

# 暴露端口
EXPOSE 5678

# 环境变量
ENV DASHBOARD_MONITOR_INTERVAL=30

# 启动命令
CMD ["python", "webapp/app.py", "--host", "0.0.0.0", "--port", "5678"]
