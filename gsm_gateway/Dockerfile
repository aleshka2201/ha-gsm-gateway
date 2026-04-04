ARG BUILD_FROM
FROM $BUILD_FROM

ENV LANG=C.UTF-8
ENV PYTHONUNBUFFERED=1

RUN apk add --no-cache \
    gcc \
    musl-dev \
    python3-dev \
    libffi-dev

RUN pip3 install --no-cache-dir --break-system-packages \
    aiomqtt==2.3.0 \
    pyserial-asyncio==0.6 \
    pyyaml==6.0.2

COPY run.sh       /run.sh
COPY gen_config.py /gen_config.py
COPY gateway.py   /gateway.py
COPY webui.py     /webui.py

RUN chmod +x /run.sh

CMD ["/run.sh"]
