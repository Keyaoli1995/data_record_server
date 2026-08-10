FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TCP_HOST=0.0.0.0 \
    TCP_PORT=30050 \
    DATA_DIR=/data \
    READ_BUFFER_BYTES=4096

WORKDIR /app

COPY data_record_server/ /app/data_record_server/

EXPOSE 30050

CMD ["python", "-m", "data_record_server"]
