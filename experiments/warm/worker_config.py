"""Operator-owned worker limits, frozen per invocation rather than chosen by agents."""
import hashlib
import json
import os
from pathlib import Path

from scheduling_policy import validate_config as scheduler_config

LEGACY = {
    'main': {'cpu_millis': 2000, 'memory_mib': 6144},
    'db': {'cpu_millis': 500, 'memory_mib': 768},
    'pool': {'cpu_millis': 500, 'memory_mib': 256},
    'proxy': {'cpu_millis': 500, 'memory_mib': 128},
}
BUILDER = {'cpu_millis': 2000, 'memory_mib': 6144}


def validate(value):
    keys = {'version', 'scheduler', 'max_parallel', 'execution_seconds', 'limits', 'workspace_mib'}
    if not isinstance(value, dict) or set(value) != keys or type(value['version']) is not int or value['version'] != 1:
        raise ValueError('Invalid worker configuration schema')
    scheduler_config(value['scheduler'])
    if not {'disk_mib', 'disk_floor_mib'} <= set(value['scheduler']):
        raise ValueError('Worker requires disk reservations and a free-space floor')
    for key, maximum in (('max_parallel', value['scheduler']['max_running']), ('execution_seconds', 86400), ('workspace_mib', value['scheduler']['disk_mib'])):
        if type(value[key]) is not int or not 1 <= value[key] <= maximum:
            raise ValueError('Invalid worker ' + key)
    if not isinstance(value['limits'], dict) or set(value['limits']) != set(LEGACY):
        raise ValueError('Limits must cover main, db, pool, and proxy containers')
    for limits in value['limits'].values():
        if not isinstance(limits, dict) or set(limits) != {'cpu_millis', 'memory_mib'}:
            raise ValueError('Invalid container limits')
        for key in limits:
            if type(limits[key]) is not int or limits[key] < 1:
                raise ValueError('Container limits must be positive integers')
    peak = {key: max(BUILDER[key], sum(role[key] for role in value['limits'].values())) for key in BUILDER}
    if any(peak[key] > value['scheduler'][key] for key in peak):
        raise ValueError('Worker capacity cannot fit one journey or dependency build')
    return value


def load(root):
    path = root / 'worker-config.json'
    if not path.exists():
        return None  # Existing pilot installations stay exclusive until configured.
    value = validate(json.loads(path.read_text()))
    if value['scheduler']['cpu_millis'] > (os.cpu_count() or 1) * 1000:
        raise ValueError('Configured CPU exceeds worker hardware')
    memory = Path('/proc/meminfo')
    if memory.exists():
        total = int(next(line.split()[1] for line in memory.read_text().splitlines() if line.startswith('MemTotal:'))) // 1024
        if value['scheduler']['memory_mib'] > total - 1024:
            raise ValueError('Worker must leave at least 1 GiB RAM outside admitted containers')
    return value


def identity(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def limits(submitted, role='main'):
    config = submitted.get('worker_config')
    return (validate(config)['limits'] if config else LEGACY)[role]


def docker_limits(submitted, role='main'):
    value = limits(submitted, role)
    return ['--cpus=' + str(value['cpu_millis'] / 1000),
            '--memory=' + str(value['memory_mib']) + 'm',
            '--memory-swap=' + str(value['memory_mib']) + 'm']


def execution_seconds(submitted, legacy=1200):
    config = submitted.get('worker_config')
    return validate(config)['execution_seconds'] if config else legacy


def demand(submitted, *, cold=False):
    config = validate(submitted['worker_config'])
    workflow = submitted.get('workflow', 'surface')
    roles = ['main']
    if workflow == 'journey' or workflow == 'suite' and submitted['suite']['action'] != 'plan':
        roles += ['db', 'pool', 'proxy']
    value = {key: sum(config['limits'][role][key] for role in roles) for key in BUILDER}
    exclusive = []
    if cold:
        exclusive.append('dependency-builder')
        value = {key: max(value[key], BUILDER[key]) for key in BUILDER}
    if workflow == 'docker' and submitted['docker']['request']['kind'] == 'build':
        exclusive.append('docker-builder')
        value = dict(BUILDER)
    return value | {'disk_mib': config['workspace_mib'], 'exclusive': exclusive}
