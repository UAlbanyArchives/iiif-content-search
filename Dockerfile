FROM python:3.10-slim

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app/ ./app
COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh

CMD ["./entrypoint.sh"]
