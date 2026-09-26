#!/usr/bin/env node
// docker-proxy.mjs — Docker API proxy on a unix socket.
// Rewrites bind/mount sources under SRC_PREFIX -> DST_PREFIX, labels containers
// with pandora.run, sets CgroupParent (with fallback), and 403s host-root or
// docker-socket binds. Passes through streaming + HTTP upgrade byte streams.
import net from 'node:net';
import fs from 'node:fs';

const LISTEN = process.env.PROXY_SOCK || '/tmp/pandora-poc/proxy.sock';
const UPSTREAM = process.env.DOCKER_SOCK || '/Users/you/.docker/run/docker.sock';
const SRC_PREFIX = process.env.SRC_PREFIX || '/tmp/pandora-poc/fakelocal';
const DST_PREFIX = process.env.DST_PREFIX || '/tmp/pandora-poc/runs/r1';
const RUN_ID = process.env.RUN_ID || 'r1';
const CGROUP_PARENT = process.env.CGROUP_PARENT || 'pandora-r1.slice';
const SOCKET_PATHS = new Set(['/var/run/docker.sock', '/run/docker.sock', UPSTREAM, '/Users/you/.docker/run/docker.sock']);

const log = (...a) => console.error(new Date().toISOString(), ...a);

function rewriteSource(src) {
  if (src === '/' || SOCKET_PATHS.has(src)) {
    const e = new Error(`pandora-proxy: refused bind of ${src}`);
    e.forbidden = true;
    throw e;
  }
  if (src === SRC_PREFIX || src.startsWith(SRC_PREFIX + '/'))
    return DST_PREFIX + src.slice(SRC_PREFIX.length);
  return src;
}

function rewriteCreate(body) {
  const hc = body.HostConfig || (body.HostConfig = {});
  if (Array.isArray(hc.Binds))
    hc.Binds = hc.Binds.map((b) => {
      const i = b.indexOf(':');
      const src = i === -1 ? b : b.slice(0, i);
      const rest = i === -1 ? '' : b.slice(i);
      return rewriteSource(src) + rest;
    });
  if (Array.isArray(hc.Mounts))
    for (const m of hc.Mounts)
      if (m.Source) m.Source = rewriteSource(m.Source);
  hc.CgroupParent = CGROUP_PARENT;
  hc.Labels = { ...(hc.Labels || {}), 'pandora.run': RUN_ID };
  body.Labels = { ...(body.Labels || {}), 'pandora.run': RUN_ID };
  return body;
}

const server = net.createServer((client) => {
  const upstream = net.connect(UPSTREAM);
  let buffer = Buffer.alloc(0);
  let handled = false;
  let waiting = false;

  const fail = (code, msg) => {
    client.end(
      `HTTP/1.1 ${code} Error\r\nContent-Type: application/json\r\nContent-Length: ${Buffer.byteLength(msg)}\r\n\r\n${msg}`,
    );
    upstream.destroy();
  };

  const onChunk = (chunk) => {
    if (handled || waiting) return handled ? upstream.write(chunk) : undefined;
    buffer = Buffer.concat([buffer, chunk]);
    const headEnd = buffer.indexOf('\r\n\r\n');
    if (headEnd === -1) return;
    const head = buffer.slice(0, headEnd).toString('latin1');
    const [requestLine, ...headerLines] = head.split('\r\n');
    if (!/^(GET|POST|PUT|DELETE|HEAD|OPTIONS|PATCH) \//.test(requestLine))
      log('NON-REST preface:', JSON.stringify(head.slice(0, 80)));
    const headers = {};
    for (const l of headerLines) {
      const i = l.indexOf(':');
      headers[l.slice(0, i).trim().toLowerCase()] = l.slice(i + 1).trim();
    }
    const isCreate = /^POST \/v[\d.]+\/containers\/create/.test(requestLine);
    if (/^POST/.test(requestLine)) log('REQ', requestLine, 'create=', isCreate);
    const isUpgrade = (headers['connection'] || '').toLowerCase().includes('upgrade');
    if (!isCreate || isUpgrade) {
      handled = true;
      upstream.write(buffer);
      return pipe();
    }
    // Buffer the full JSON body for containers/create.
    const bodyStart = headEnd + 4;
    const chunked = (headers['transfer-encoding'] || '').toLowerCase().includes('chunked');
    const want = chunked ? null : parseInt(headers['content-length'] || '0', 10);
    const complete = () =>
      chunked ? buffer.includes('0\r\n\r\n', bodyStart) : buffer.length - bodyStart >= want;
    if (!complete()) {
      waiting = true;
      const onData = (c) => {
        buffer = Buffer.concat([buffer, c]);
        if (complete()) {
          client.off('data', onData);
          finish();
        }
      };
      client.on('data', onData);
      return;
    }
    finish();

    function finish() {
      handled = true;
      let bodyBuf = buffer.slice(bodyStart);
      let json;
      if (chunked) {
        // Decode chunked body.
        let out = [], pos = 0;
        for (;;) {
          const eol = bodyBuf.indexOf('\r\n', pos);
          const size = parseInt(bodyBuf.slice(pos, eol).toString(), 16);
          if (size === 0) break;
          out.push(bodyBuf.slice(eol + 2, eol + 2 + size));
          pos = eol + 2 + size + 2;
        }
        bodyBuf = Buffer.concat(out);
      }
      try {
        json = rewriteCreate(JSON.parse(bodyBuf.toString('utf8')));
      } catch (e) {
        if (e.forbidden)
          return fail(403, JSON.stringify({ message: e.message }));
        log('create rewrite error, passing through:', e.message);
        upstream.write(buffer);
        return pipe();
      }
      const newBody = Buffer.from(JSON.stringify(json));
      const newHead = headerLines
        .filter((l) => !/^(content-length|transfer-encoding):/i.test(l))
        .join('\r\n');
      upstream.write(
        `${requestLine}\r\n${newHead}\r\nContent-Length: ${newBody.length}\r\n\r\n`,
      );
      upstream.write(newBody);
      pipe();
    }
  };
  client.on('data', onChunk);

  function pipe() {
    client.off('data', onChunk);
    client.pipe(upstream);
    upstream.pipe(client);
  }
  client.on('error', () => upstream.destroy());
  upstream.on('error', (e) => {
    log('upstream error:', e.message);
    client.destroy();
  });
  client.on('close', () => upstream.destroy());
  upstream.on('close', () => client.destroy());
});

fs.rmSync(LISTEN, { force: true });
server.listen(LISTEN, () => log(`proxy on ${LISTEN} -> ${UPSTREAM} (src ${SRC_PREFIX} -> ${DST_PREFIX}, run=${RUN_ID})`));
