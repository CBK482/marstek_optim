ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-debian:bookworm
FROM ${BUILD_FROM}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data

# numpy + scipy (HiGHS LP/MILP solver) from Debian: pre-built for all arches.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-numpy python3-scipy ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Pure-python deps in a venv that can still see the system numpy/scipy.
COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv --system-site-packages /opt/venv \
    && /opt/venv/bin/pip install -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /app
COPY app /app/app
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
