"""Bounded argv recognition; never parse or rewrite arbitrary shell text."""
import shlex
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent / "warm"))
from journey import journey_config
from workflow_options import APPS, surface_selectors
from validation_request import validate_request


def _validation_command(argv):
    """Strip pnpm's optional script wrapper without accepting shell syntax."""
    return argv[1:] if argv[:1] == ['run'] else argv


ALIASES = {
    'test:unit': 'unit', 'test:tools': 'tools', 'test': 'full',
    'test:browser-integration': 'browser-integration',
    'test:employee-browser': 'employee-browser',
    'test:mockup-browser': 'mockup-browser',
    'test:postgres': 'postgres',
}
VALIDATION_SUITES = frozenset({
    'unit', 'tools', 'full', 'agent-web', 'employee-browser',
    'browser-integration', 'mockup-browser', 'postgres',
})


def _validation_parts(argv):
    command = _validation_command(argv)
    if not command:
        return None, []
    if command[:1] == ['validate']:
        command = command[1:]
        if not command:
            return None, []
        return (command[0] if command[0] in VALIDATION_SUITES else None), command[1:]
    return ALIASES.get(command[0]), command[1:]


def validation_request(argv):
    """Return normalized metadata for a supported remote validation command."""
    suite, args = _validation_parts(argv)
    if suite is None:
        raise ValueError('Not a validation command')
    return validate_request({'version': 1, 'suite': suite, 'args': args})


def _validation_route(argv):
    """Classify recognized validation commands before ordinary local fallback."""
    suite, args = _validation_parts(argv)
    if suite is None:
        return None
    literal_tools = _validation_command(argv)[:1] == ['test:tools']
    try:
        request = validation_request(argv)
    except ValueError:
        if suite in {'unit', 'tools'}:
            if suite == 'tools' and literal_tools and args:
                return 'reject', [], 'pnpm test:tools runs the broad suite; use pnpm validate tools <test files> for focused local validation. No validation started.'
            return 'local', [], ''
        if suite == 'postgres':
            return 'reject', [], 'Use pnpm test:postgres <api|scenarios> [--foundation-only for api]. No validation started.'
        return 'reject', [], f'{suite} currently runs as one complete suite and takes no selectors. No validation started.'
    return 'validation', [], ''


def suite_request(argv, shard_count):
    """Return the bounded suite request represented by a recognized pnpm argv."""
    command = argv[1:] if argv[:1] == ['run'] else argv
    journey = command[1:] if command[:1] == ['validate'] else command
    if journey[:1] != ['journeys']:
        raise ValueError('Not a suite command')
    options = journey[1:]
    if len(options) != len(set(options)) or any(option not in {'--keep-going', '--update'} for option in options):
        raise ValueError('Use pnpm journeys [--update] [--keep-going]. No validation started.')
    keep_going = '--keep-going' in options
    update = '--update' in options
    return {'action': 'run', 'shard_count': shard_count, 'selection': None,
            'keep_going': keep_going, 'update': update}


def surface_suite_request(argv, shard_count):
    """Return a bounded surface-run request without changing the public argv."""
    command = argv[1:] if argv[:1] == ['run'] else argv
    if command[:1] == ['test:surface']:
        if len(command) < 2 or command[1] not in APPS:
            raise ValueError('Use pnpm test:surface <web|desk> [files] [--grep PATTERN] [--keep-going].')
        app, options = command[1], command[2:]
    elif command[:2] == ['validate', 'surface']:
        if len(command) < 3 or command[2] not in APPS:
            raise ValueError('Use pnpm validate surface <web|desk> [files] [--grep PATTERN] [--keep-going].')
        app, options = command[2], command[3:]
    else:
        raise ValueError('Not a surface command')
    selectors, keep_going = surface_suite_options(options)
    return {'action': 'run', 'app': app, 'selectors': selectors,
            'shard_count': shard_count, 'keep_going': keep_going}


def surface_suite_options(options):
    """Remove only a standalone keep-going option, never a grep pattern."""
    selectors = []
    keep_going = False
    index = 0
    while index < len(options):
        option = options[index]
        if option == '--grep':
            selectors.append(option)
            if index + 1 < len(options):
                selectors.append(options[index + 1])
            index += 2
            continue
        if option == '--keep-going':
            if keep_going:
                raise ValueError('Use at most one --keep-going for surface validation.')
            keep_going = True
            index += 1
            continue
        selectors.append(option)
        index += 1
    return surface_selectors(selectors), keep_going


def classify(argv, treatment='normal'):
    validation = _validation_route(argv)
    if validation is not None:
        return validation
    command = argv[1:] if argv[:1] == ['run'] else argv
    journey = command[1:] if command[:1] == ['validate'] else command
    if journey[:1] in (['journey'], ['journeys']):
        if journey[:1] == ['journey']:
            try:
                journey_config({'selectors': journey[1:]})
                return 'journey', journey[1:], ''
            except ValueError as error:
                return 'reject', [], str(error) + '. No validation started.'
        try:
            # The shard count is supplied by the private launcher. Parsing it
            # here would make ordinary `pnpm journeys` depend on environment.
            suite_request(argv, 1)
            return 'suite-run', [], ''
        except ValueError as error:
            return 'reject', [], str(error)
    if command[:1] == ['test:surface']:
        if len(command) < 2 or command[1] not in APPS:
            return 'reject', [], 'Use pnpm test:surface <web|desk> [files] [--grep PATTERN].'
        selectors = command[2:]
    elif command[:2] == ['validate', 'surface']:
        if len(command) < 3 or command[2] not in APPS:
            return 'reject', [], 'Use pnpm validate surface <web|desk> [files] [--grep PATTERN].'
        selectors = command[3:]
    elif command[:3] == ['--filter', '@acme/web', 'test:e2e']:
        if treatment == 'normal':
            return 'local', [], ''
        # Only known ordinary selectors and exact trial runner flags are recognized.
        selectors = [x for x in command[3:] if x not in {'--workers=1', '--reporter=line,junit'}]
        if any(x.startswith('-') for x in selectors):
            return 'reject', [], 'This trial supports file selectors only. No validation started.'
        alternative = shlex.join(['pnpm', 'test:surface', 'web', *selectors])
        if treatment == 'block':
            return 'reject', [], 'This direct surface entry is blocked in this trial. Run: ' + alternative
    else:
        return 'local', [], ''
    try:
        surface, _ = surface_suite_options(selectors)
        return 'remote', surface, ''
    except ValueError as error:
        return 'reject', [], str(error) + '. No validation started.'


def selected_surface(argv):
    command = argv[1:] if argv[:1] == ['run'] else argv
    if command[:1] == ['test:surface']:
        return command[1]
    if command[:2] == ['validate', 'surface']:
        return command[2]
    return 'web'  # Historical direct-package experiment treatment.
