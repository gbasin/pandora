#!/usr/bin/env python3
"""One-job GitHub runners for Pandora's e2e workflows, each in its own Incus VM.

Runs on the shared host as root, because it drives Incus. Job code never runs on
the host: every runner lives in an ephemeral VM launched from the prepared image,
takes one job, powers off, and Incus deletes the VM. The VM holds its own Pandora
worker (Incus, the pool, `pandora-ci` with sudo), so the job's root is the VM's.

    e2e_pool.py run   [--config F]     the daemon: keep the pool, forever
    e2e_pool.py once  [--config F]     one pass, then print the status
    e2e_pool.py drain [--config F]     delete idle VMs and their registrations
    e2e_pool.py acl --exclude CIDR...  the host bridge's egress ACL as JSON

The daemon keeps no state of its own. Creation, start and busy times live as
`user.e2e.*` keys on each VM, so a restart adopts the running VMs, and stopping
the daemon never interrupts a job.

The GitHub token (fine-grained, this repository only, Administration read and
write) is read from `token_file` for each request and never logged. The client
makes exactly three calls: mint a JIT runner configuration, list runners, and
delete a runner whose name this pool owns.
"""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
import tomllib
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULTS = {
    'repository': 'gbasin/pandora',
    'labels': ['self-hosted', 'Linux', 'X64', 'e2e'],
    'project': 'pandora-e2e',
    'image': 'pandora-e2e',
    'profile': 'pandora-e2e',
    'idle_target': 1,
    'max_vms': 2,
    'interval_seconds': 15,
    'start_timeout_seconds': 600,
    'gone_grace_seconds': 180,
    'idle_max_seconds': 6 * 3600,
    'job_max_seconds': 2 * 3600,
    'token_file': '/etc/pandora-e2e/github-token',
    'status_file': '/run/pandora-e2e/status.json',
}
NAME = re.compile(r'^e2e-[0-9a-f]{12}$')
CREATED, STARTED, BUSY = 'user.e2e.created', 'user.e2e.started', 'user.e2e.busy'
START_RUNNER = '/opt/pandora-e2e/start-runner'
API = 'https://api.github.com'
JIT_LIMIT = 1024 * 1024


class PoolError(RuntimeError):
    pass


def load_config(path):
    config = dict(DEFAULTS)
    if path and Path(path).exists():
        with open(path, 'rb') as file:
            raw = tomllib.load(file)
        unknown = sorted(set(raw) - set(DEFAULTS))
        if unknown:
            raise PoolError('unknown config keys: %s' % ', '.join(unknown))
        config.update(raw)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', config['repository']):
        raise PoolError('repository must be owner/name')
    if config['max_vms'] < 1 or config['idle_target'] > config['max_vms']:
        raise PoolError('need 1 <= max_vms and idle_target <= max_vms')
    return config


# --- GitHub ------------------------------------------------------------------

class GitHub:
    """The pool's three runner calls, for one repository, and nothing else."""

    def __init__(self, repository, token_file, labels, opener=urlopen):
        self.repository, self.token_file, self.labels = repository, token_file, labels
        self.opener = opener

    def _token(self):
        token = Path(self.token_file).read_text().strip()
        if not token or any(c.isspace() for c in token):
            raise PoolError('the token file %s is empty or malformed' % self.token_file)
        return token

    def _call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = Request(API + '/repos/' + self.repository + path, data=data, method=method,
                          headers={'Authorization': 'Bearer ' + self._token(),
                                   'Accept': 'application/vnd.github+json',
                                   'X-GitHub-Api-Version': '2022-11-28',
                                   'Content-Type': 'application/json',
                                   'User-Agent': 'pandora-e2e-pool'})
        try:
            with self.opener(request, timeout=30) as response:
                raw = response.read()
        except HTTPError as error:
            code = error.code
            error.close()
            raise PoolError('GitHub %s %s answered HTTP %d' % (method, path.split('?')[0], code)) \
                from None
        except (URLError, TimeoutError, OSError):
            raise PoolError('GitHub %s %s did not answer' % (method, path.split('?')[0])) from None
        return json.loads(raw) if raw else None

    def runners(self):
        """{name: runner} for every registered runner whose name this pool owns."""
        found, page = {}, 1
        while True:
            reply = self._call('GET', '/actions/runners?per_page=100&page=%d' % page)
            batch = reply.get('runners') or []
            for runner in batch:
                if NAME.match(runner.get('name') or ''):
                    found[runner['name']] = runner
            if len(batch) < 100 or page >= 20:
                return found
            page += 1

    def jit(self, name):
        if not NAME.match(name):
            raise PoolError('refusing to mint a runner named %r' % name)
        reply = self._call('POST', '/actions/runners/generate-jitconfig',
                           {'name': name, 'runner_group_id': 1, 'labels': self.labels,
                            'work_folder': '_work'})
        config = (reply or {}).get('encoded_jit_config')
        if not isinstance(config, str) or not config or len(config) > JIT_LIMIT \
                or not re.fullmatch(r'[A-Za-z0-9+/=]+', config):
            raise PoolError('GitHub returned an invalid JIT configuration')
        return config

    def remove(self, runner):
        if not NAME.match(runner.get('name') or '') or not isinstance(runner.get('id'), int):
            raise PoolError('refusing to delete runner %r' % runner.get('name'))
        self._call('DELETE', '/actions/runners/%d' % runner['id'])


# --- Incus -------------------------------------------------------------------

class Incus:
    def __init__(self, project, run=subprocess.run):
        self.project, self._run = project, run

    def __call__(self, *args, stdin=None, timeout=120, check=True):
        proc = self._run(['incus', '--project', self.project, *args], input=stdin,
                         capture_output=True, timeout=timeout)
        if check and proc.returncode != 0:
            err = (proc.stderr or b'').decode('utf-8', 'replace').strip()[:400]
            raise PoolError('incus %s failed: %s' % (args[0], err))
        return proc

    def vms(self):
        proc = self('list', '--format', 'json', timeout=60)
        return [vm for vm in json.loads(proc.stdout or b'[]') if NAME.match(vm.get('name') or '')]

    def launch(self, image, name, profile, now):
        self('launch', image, name, '--vm', '--ephemeral', '--profile', profile,
             '--config', '%s=%d' % (CREATED, now), timeout=600)

    def agent_ready(self, name):
        try:
            return self('exec', name, '--', 'true', timeout=30, check=False).returncode == 0
        except subprocess.TimeoutExpired:
            return False

    def start_runner(self, name, jit):
        self('exec', name, '--', START_RUNNER, stdin=jit.encode(), timeout=60)

    def mark(self, name, key, now):
        self('config', 'set', name, '%s=%d' % (key, now), timeout=60)

    def delete(self, name):
        self('delete', '--force', name, timeout=300, check=False)


# --- the pool ------------------------------------------------------------------

def stamp(vm, key):
    value = (vm.get('config') or {}).get(key, '')
    return int(value) if str(value).isdecimal() else None


class Pool:
    def __init__(self, config, incus, github, clock=time.time, sleep=time.sleep, say=print):
        self.config, self.incus, self.github = config, incus, github
        self.clock, self.sleep, self.say = clock, sleep, say

    def retire(self, name, runner, why):
        self.say('retire %s: %s' % (name, why))
        self.incus.delete(name)
        if runner is not None:
            try:
                self.github.remove(runner)
            except PoolError as error:
                self.say('retire %s: %s' % (name, error))

    def launch(self):
        name = 'e2e-' + secrets.token_hex(6)
        now = int(self.clock())
        self.say('launch %s' % name)
        minted = False
        try:
            self.incus.launch(self.config['image'], name, self.config['profile'], now)
            deadline = now + self.config['start_timeout_seconds']
            while not self.incus.agent_ready(name):
                if self.clock() > deadline:
                    raise PoolError('the VM agent did not answer')
                self.sleep(3)
            jit = self.github.jit(name)
            minted = True
            self.incus.start_runner(name, jit)
            del jit
            self.incus.mark(name, STARTED, int(self.clock()))
            return name
        except (PoolError, subprocess.TimeoutExpired) as error:
            self.say('launch %s failed: %s' % (name, error))
            self.incus.delete(name)
            if minted:
                try:
                    runner = self.github.runners().get(name)
                    if runner is not None:
                        self.github.remove(runner)
                except PoolError as cleanup:
                    self.say('launch %s: %s' % (name, cleanup))
            return None

    def step(self):
        """One pass: retire what is done or stuck, then refill. Returns the status."""
        now = int(self.clock())
        cfg = self.config
        vms = self.incus.vms()
        runners = self.github.runners()
        live, idle, busy = set(), 0, 0
        for vm in vms:
            name = vm['name']
            runner = runners.get(name)
            created, started, since = stamp(vm, CREATED), stamp(vm, STARTED), stamp(vm, BUSY)
            if vm.get('status') not in ('Running', 'Starting', None) and started is not None:
                self.retire(name, runner, 'status %s' % vm.get('status'))
                continue
            if started is None:
                if created is None or now - created > cfg['start_timeout_seconds']:
                    self.retire(name, runner, 'its runner never started')
                    continue
                live.add(name)
                idle += 1
                continue
            if runner is not None and runner.get('busy'):
                if since is None:
                    self.incus.mark(name, BUSY, now)
                    since = now
                if now - since > cfg['job_max_seconds']:
                    self.retire(name, runner, 'its job ran past %ds' % cfg['job_max_seconds'])
                    continue
                live.add(name)
                busy += 1
                continue
            if since is not None or (runner is None and now - started > cfg['gone_grace_seconds']):
                self.retire(name, runner, 'its job is over')
                continue
            if now - started > cfg['idle_max_seconds']:
                self.retire(name, runner, 'idle past %ds' % cfg['idle_max_seconds'])
                continue
            live.add(name)
            idle += 1
        for name, runner in runners.items():
            if name not in live and not any(vm['name'] == name for vm in vms):
                self.say('deregister %s: its VM is gone' % name)
                try:
                    self.github.remove(runner)
                except PoolError as error:
                    self.say('deregister %s: %s' % (name, error))
        launched = []
        while idle < cfg['idle_target'] and len(live) < cfg['max_vms']:
            name = self.launch()
            if name is None:
                break
            launched.append(name)
            live.add(name)
            idle += 1
        return {'at': now, 'vms': sorted(live), 'idle': idle, 'busy': busy,
                'launched': launched}

    def drain(self):
        runners = self.github.runners()
        for vm in self.incus.vms():
            runner = runners.get(vm['name'])
            if runner is None or not runner.get('busy'):
                self.retire(vm['name'], runner, 'drain')


def write_status(path, status):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(status, sort_keys=True) + '\n')
    tmp.replace(path)


# --- the host bridge's ACL ---------------------------------------------------

NON_PUBLIC = ('0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8', '169.254.0.0/16',
              '172.16.0.0/12', '192.0.0.0/24', '192.0.2.0/24', '192.168.0.0/16',
              '198.18.0.0/15', '198.51.100.0/24', '203.0.113.0/24', '224.0.0.0/4',
              '240.0.0.0/4')


def public_ranges(exclude=()):
    ranges = [ipaddress.ip_network('0.0.0.0/0')]
    for blocked in [*map(ipaddress.ip_network, NON_PUBLIC), *exclude]:
        ranges = [part for net in ranges
                  for part in ((net,) if not net.overlaps(blocked) else
                               () if net.subnet_of(blocked) else net.address_exclude(blocked))]
    return sorted(ranges)


def acl(exclude):
    """Egress to public IPv4 only, minus the host's own public addresses.

    Incus applies reject rules before allow rules, so the ACL lists only what is
    allowed and the bridge rejects the rest by default. Incus still answers DHCP
    and DNS on the bridge address.
    """
    nets = [ipaddress.ip_network(value, strict=False) for value in exclude]
    if not nets or any(net.version != 4 for net in nets):
        raise PoolError("exclude the host's own public IPv4 addresses")
    return {'description': 'Pandora e2e VMs: the public internet, not the host or its VMs',
            'egress': [{'action': 'allow', 'state': 'enabled',
                        'description': "Public IPv4 except the host's addresses",
                        'destination': ','.join(map(str, public_ranges(nets)))}],
            'ingress': []}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='verb', required=True)
    for verb in ('run', 'once', 'drain'):
        sub.add_parser(verb).add_argument('--config', default='/etc/pandora-e2e/pool.toml')
    sub.add_parser('acl').add_argument('--exclude', action='append', default=[], metavar='CIDR')
    args = parser.parse_args(argv)
    if args.verb == 'acl':
        print(json.dumps(acl(args.exclude), indent=2))
        return 0
    config = load_config(args.config)
    pool = Pool(config, Incus(config['project']),
                GitHub(config['repository'], config['token_file'], config['labels']),
                say=lambda text: print(text, flush=True))
    if args.verb == 'drain':
        pool.drain()
        return 0
    while True:
        try:
            status = pool.step()
            write_status(config['status_file'], status)
        except (PoolError, subprocess.TimeoutExpired, ValueError) as error:
            print('pass failed: %s' % error, file=sys.stderr, flush=True)
            if args.verb == 'once':
                return 1
        if args.verb == 'once':
            print(json.dumps(status, indent=1, sort_keys=True))
            return 0
        time.sleep(config['interval_seconds'])


if __name__ == '__main__':
    os.umask(0o077)
    sys.exit(main())
