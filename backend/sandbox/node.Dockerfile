FROM node:22-slim

ENV HOME=/tmp/npm-home \
    NPM_CONFIG_CACHE=/tmp/npm-cache \
    NPM_CONFIG_LOGS_DIR=/tmp/npm-logs \
    NPM_CONFIG_REGISTRY=https://registry.npmjs.org/ \
    NPM_CONFIG_OFFLINE=true \
    NPM_CONFIG_AUDIT=false \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_IGNORE_SCRIPTS=true \
    NPM_CONFIG_INSTALL_LINKS=true
