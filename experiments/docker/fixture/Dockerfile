FROM node:24-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6
WORKDIR /workspace
COPY . .
RUN node -e "if(require('fs').existsSync('ignored.txt')) throw Error('Docker ignore was not applied')"
CMD ["node", "check.mjs"]
