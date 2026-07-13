FROM python:3.9-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY cloud115.py .
COPY wechat_work.py .
COPY douban.py .
COPY baidu_forum.py .
COPY media/ media/
COPY templates/ templates/
COPY VERSION .

VOLUME /app/data

EXPOSE 3698

CMD ["python", "app.py"]
