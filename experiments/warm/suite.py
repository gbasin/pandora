"""Private suite execution requests; normal pnpm suite routing is not enabled yet."""
import json
import re
from suite_evidence import validate_plan

ID = re.compile(r'(?:S[0-6]|SX)-[0-9]{2}\Z')


def suite_request(value):
    if not isinstance(value, dict):
        raise ValueError('Suite request must be an object')
    action = value.get('action')
    if action == 'run':
        if set(value) not in ({'action', 'shard_count', 'selection', 'keep_going'}, {'action', 'shard_count', 'selection', 'keep_going', 'update'}) or type(value.get('keep_going')) is not bool or ('update' in value and type(value['update']) is not bool):
            raise ValueError('Suite run requires shard_count, selection, boolean keep_going and optional boolean update')
        suite_request({key: item for key, item in value.items() if key not in {'keep_going', 'update'}} | {'action': 'plan'})
    elif action == 'plan':
        if set(value) != {'action', 'shard_count', 'selection'}:
            raise ValueError('Plan request requires action, shard_count and selection')
        count = value['shard_count']
        if type(count) is not int or not 1 <= count <= 32:
            raise ValueError('Shard count must be an integer from 1 through 32')
        selection = value['selection']
        if selection is not None and (not isinstance(selection, list) or not selection or
                any(not isinstance(x, str) or not ID.fullmatch(x) for x in selection) or
                len(set(selection)) != len(selection)):
            raise ValueError('Suite selection must be null or unique scenario IDs')
    elif action == 'shard':
        if set(value) != {'action', 'plan', 'shard'}:
            raise ValueError('Shard request requires action, plan and shard')
        plan = validate_plan(value['plan'])
        if type(value['shard']) is not int or not 1 <= value['shard'] <= len(plan['shards']):
            raise ValueError('Shard index is outside the frozen plan')
    else:
        raise ValueError('Suite action must be plan or shard; updates are not enabled')
    return value


def suite_config(submitted):
    request = suite_request(submitted['suite'])
    source = submitted['source_digest']
    if not isinstance(source, str) or not re.fullmatch('[0-9a-f]{64}', source):
        raise ValueError('Invalid suite source identity')
    requested_update = submitted.get('suite_update', False)
    if type(requested_update) is not bool:
        raise ValueError('Suite update mode must be boolean')
    if request['action'] == 'shard' and request['plan']['source_digest'] != source:
        raise ValueError('Source differs from frozen suite plan; no shard started')
    return request | {'source_digest': source, 'update': requested_update if request['action'] == 'shard' else False}


def suite_command(config):
    return ['sudo', 'docker', 'exec', '-e',
            'PANDORA_SUITE_CONFIG=' + json.dumps(config, separators=(',', ':')),
            '-w', '/workspace/source/packages/scenarios',
            'pandora-warm-' + config['attempt'],
            'node', '--import', 'tsx', '/workspace/source/pandora-suite.mjs']


def validate_result(stage, submitted, terminal, manifest):
    """Bind returned plan or shard evidence to the immutable request."""
    from suite_evidence import validate_shard
    config = suite_config(submitted)
    name = 'results/suite-' + config['action'] + '.json'
    if name not in manifest:
        if terminal['exit_code'] == 0:
            raise ValueError('Successful suite run lacks its declared evidence')
        return None  # Failed infrastructure may only have logs; never aggregatable.
    result = json.loads((stage / name).read_text())
    if config['action'] == 'plan':
        validate_plan(result)
        if (result['source_digest'] != config['source_digest'] or
                result['selection'] != config['selection'] or
                len(result['shards']) != config['shard_count']):
            raise ValueError('Returned suite plan differs from its request')
    else:
        validate_shard(config['plan'], result)
        if result['shard'] != config['shard'] or result['exit_code'] != terminal['exit_code']:
            raise ValueError('Returned shard differs from request or terminal status')
    return result
