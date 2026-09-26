FROM node:24-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6 AS build
RUN npm install --global pnpm@12.3.3
WORKDIR /workspace
ENV CI=true
COPY ["apps/agent/package.json", "./apps/agent/package.json"]
COPY ["apps/api/package.json", "./apps/api/package.json"]
COPY ["apps/web/package.json", "./apps/web/package.json"]
COPY ["apps/web/package.json", "./apps/web/package.json"]
COPY ["apps/brand-tour/package.json", "./apps/brand-tour/package.json"]
COPY ["apps/brief/package.json", "./apps/brief/package.json"]
COPY ["apps/desk/package.json", "./apps/desk/package.json"]
COPY ["apps/film/package.json", "./apps/film/package.json"]
COPY ["apps/mockup/package.json", "./apps/mockup/package.json"]
COPY ["apps/progress/package.json", "./apps/progress/package.json"]
COPY ["apps/static-docs/package.json", "./apps/static-docs/package.json"]
COPY ["apps/web/package.json", "./apps/web/package.json"]
COPY ["legacy/apps/blueprint-viewer/package.json", "./legacy/apps/blueprint-viewer/package.json"]
COPY ["legacy/apps/app-api/package.json", "./legacy/apps/app-api/package.json"]
COPY ["legacy/apps/app-web/package.json", "./legacy/apps/app-web/package.json"]
COPY ["legacy/apps/intake-lab/package.json", "./legacy/apps/intake-lab/package.json"]
COPY ["legacy/apps/journey/package.json", "./legacy/apps/journey/package.json"]
COPY ["legacy/packages/blueprint/package.json", "./legacy/packages/blueprint/package.json"]
COPY ["legacy/packages/core/package.json", "./legacy/packages/core/package.json"]
COPY ["legacy/packages/app-ui/package.json", "./legacy/packages/app-ui/package.json"]
COPY ["package.json", "./package.json"]
COPY ["packages/client/package.json", "./packages/client/package.json"]
COPY ["packages/design/package.json", "./packages/design/package.json"]
COPY ["packages/domain/package.json", "./packages/domain/package.json"]
COPY ["packages/scenarios/package.json", "./packages/scenarios/package.json"]
COPY ["packages/vendors/package.json", "./packages/vendors/package.json"]
COPY ["packages/web-ui/package.json", "./packages/web-ui/package.json"]
COPY ["patches/decode-uri-component@0.5.0.patch", "./patches/decode-uri-component@0.5.0.patch"]
COPY ["pnpm-lock.yaml", "./pnpm-lock.yaml"]
COPY ["pnpm-workspace.yaml", "./pnpm-workspace.yaml"]
RUN --mount=type=cache,id=pandora-compiled-pnpm,target=/pnpm/store pnpm install --frozen-lockfile --store-dir=/pnpm/store
# Preserve dependency input timestamps while overlaying current application source.
RUN --mount=type=bind,source=.,target=/inputs node /inputs/pandora-copy-source.cjs && pnpm --filter @acme/web build
FROM node:24-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6
WORKDIR /workspace
COPY --from=build /workspace/apps/web/dist /workspace/dist
COPY pandora-check-build.cjs /workspace/check.cjs
CMD ["node", "check.cjs"]
