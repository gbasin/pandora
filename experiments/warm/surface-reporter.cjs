const fs = require('node:fs');

const output = process.env.PANDORA_SURFACE_REPORT;
const mode = process.env.PANDORA_SURFACE_REPORT_MODE;
const tests = new Map();
function row(test) {
  return { id: test.id, project: test.parent.project()?.name || 'default', file: test.location.file, title: test.titlePath().join(' › ') };
}
class SurfaceReporter {
  onBegin(_, suite) { this.inventory = suite.allTests().map(row); }
  onTestEnd(test, result) { tests.set(test.id, result.status); }
  onEnd(result) {
    if (!output) return;
    fs.writeFileSync(output, JSON.stringify({ mode, status: result.status, inventory: this.inventory || [],
      outcomes: [...tests].map(([id, status]) => ({ id, status })) }) + '\n');
  }
}
module.exports = SurfaceReporter;
