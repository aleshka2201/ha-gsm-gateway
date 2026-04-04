ARG BUILD_FROM
FROM $BUILD_FROM

ENV LANG=C.UTF-8
ENV PYTHONUNBUFFERED=1

RUN apk add --no-cache \
    python3 \
    py3-pip \
    py3-yaml \
    gcc \
    musl-dev \
    python3-dev \
    libffi-dev

RUN pip3 install --no-cache-dir --break-system-packages \
    aiomqtt>=2.0.0 \
    pyserial-asyncio>=0.6

COPY run.sh /run.sh
COPY gateway.py /gateway.py
COPY webui.py /webui.py
COPY gen_config.py /gen_config.py

RUN chmod +x /run.sh

CMD ["/run.sh"]
