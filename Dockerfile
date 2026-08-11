FROM python:3.12-slim

ARG COLLECTOR_UID=10001
ARG COLLECTOR_GID=10001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TCP_HOST=0.0.0.0 \
    TCP_PORT=30050 \
    DATA_DIR=/data \
    READ_BUFFER_BYTES=4096 \
    IDLE_TIMEOUT_SECONDS=30

WORKDIR /app

RUN groupadd --gid "${COLLECTOR_GID}" collector \
    && useradd --uid "${COLLECTOR_UID}" --gid collector --no-create-home \
        --shell /usr/sbin/nologin collector \
    && install -d --owner=collector --group=collector /data

COPY data_record_server/ /app/data_record_server/

EXPOSE 30050

CMD ["python", "-m", "data_record_server"]

USER collector
