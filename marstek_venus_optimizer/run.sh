#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
set -e

LOG_LEVEL="$(bashio::config 'log_level' 'info')"
export LOG_LEVEL
export DATA_DIR=/data

bashio::log.info "Starting Marstek Venus Optimizer (log level: ${LOG_LEVEL})"
bashio::log.info "Ingress port 8099 – open the 'Venus Optimizer' panel in the sidebar"

cd /app
exec python3 -m uvicorn app.main:app \
    --host 0.0.0.0 \
    --port 8099 \
    --log-level "${LOG_LEVEL}" \
    --no-access-log \
    --proxy-headers \
    --forwarded-allow-ips="*"
