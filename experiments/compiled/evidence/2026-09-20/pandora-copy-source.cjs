const fs = require('node:fs');
const path = require('node:path');
const inputs = new Set(["apps/agent/package.json", "apps/api/package.json", "apps/web/package.json", "apps/web/package.json", "apps/brand-tour/package.json", "apps/brief/package.json", "apps/desk/package.json", "apps/film/package.json", "apps/mockup/package.json", "apps/progress/package.json", "apps/static-docs/package.json", "apps/web/package.json", "legacy/apps/blueprint-viewer/package.json", "legacy/apps/app-api/package.json", "legacy/apps/app-web/package.json", "legacy/apps/intake-lab/package.json", "legacy/apps/journey/package.json", "legacy/packages/blueprint/package.json", "legacy/packages/core/package.json", "legacy/packages/app-ui/package.json", "package.json", "packages/client/package.json", "packages/design/package.json", "packages/domain/package.json", "packages/scenarios/package.json", "packages/vendors/package.json", "packages/web-ui/package.json", "patches/decode-uri-component@0.5.0.patch", "pnpm-lock.yaml", "pnpm-workspace.yaml"]);
function copy(dir = '') {
  for (const e of fs.readdirSync(path.join('/inputs', dir), {withFileTypes:true})) {
    const rel = path.join(dir,e.name);
    if (inputs.has(rel)) continue;
    const src=path.join('/inputs',rel), dst=path.join('/workspace',rel);
    if (e.isDirectory()) { fs.mkdirSync(dst,{recursive:true}); copy(rel); }
    else if (e.isSymbolicLink()) fs.symlinkSync(fs.readlinkSync(src),dst);
    else fs.copyFileSync(src,dst);
  }
}
copy();
