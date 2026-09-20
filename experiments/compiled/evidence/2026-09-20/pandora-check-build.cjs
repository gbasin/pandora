const fs = require('node:fs');
const html = fs.readFileSync('dist/index.html','utf8');
if (!html.includes('<html') || !fs.readdirSync('dist/assets').some(f=>f.endsWith('.js'))) throw Error('Missing compiled application');
const expected = process.argv[2];
if (expected && !html.includes(expected)) throw Error('Stale build: missing '+expected);
console.log('Compiled application verified'+(expected?' '+expected:''));
