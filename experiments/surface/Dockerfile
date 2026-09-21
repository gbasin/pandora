FROM node:24-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN apt-get update && apt-get install -y --no-install-recommends git python3 \
    && npm install --global pnpm@12.3.4 @playwright/test@1.62.1 \
    && playwright install --with-deps chromium webkit \
    && rm -rf /var/lib/apt/lists/*
USER node
WORKDIR /workspace
