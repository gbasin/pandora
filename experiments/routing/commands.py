"""Bounded argv recognition; never parse or rewrite arbitrary shell text."""
import shlex
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent / "warm"))
from journey import journey_config
from workflow_options import APPS, surface_selectors


def suite_request(argv, shard_count):
    """Return the bounded suite request represented by a recognized pnpm argv."""
    command = argv[1:] if argv[:1] == ['run'] else argv
    journey = command[1:] if command[:1] == ['validate'] else command
    if journey[:1] != ['journeys']:
        raise ValueError('Not a suite command')
    options = journey[1:]
    if options == []:
        keep_going = False
    elif options == ['--keep-going']:
        keep_going = True
    elif '--update' in options:
        raise ValueError('Suite updates are not routed. Run: pnpm journey <id> --update. No validation started.')
    else:
        raise ValueError('Use pnpm journeys [--keep-going]. No validation started.')
    return {'action': 'run', 'shard_count': shard_count, 'selection': None,
            'keep_going': keep_going}


def classify(argv, treatment='normal'):
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
            return 'reject', [], 'Use pnpm test:surface <borrower-web|desk> [files] [--grep PATTERN].'
        selectors = command[2:]
    elif command[:2] == ['validate', 'surface']:
        if len(command) < 3 or command[2] not in APPS:
            return 'reject', [], 'Use pnpm validate surface <borrower-web|desk> [files] [--grep PATTERN].'
        selectors = command[3:]
    elif command[:3] == ['--filter', '@eichler/borrower-web', 'test:e2e']:
        if treatment == 'normal':
            return 'local', [], ''
        # Only known ordinary selectors and exact trial runner flags are recognized.
        selectors = [x for x in command[3:] if x not in {'--workers=1', '--reporter=line,junit'}]
        if any(x.startswith('-') for x in selectors):
            return 'reject', [], 'This trial supports file selectors only. No validation started.'
        alternative = shlex.join(['pnpm', 'test:surface', 'borrower-web', *selectors])
        if treatment == 'block':
            return 'reject', [], 'This direct surface entry is blocked in this trial. Run: ' + alternative
    else:
        return 'local', [], ''
    try:
        return 'remote', surface_selectors(selectors), ''
    except ValueError as error:
        return 'reject', [], str(error) + '. No validation started.'


def selected_surface(argv):
    command = argv[1:] if argv[:1] == ['run'] else argv
    if command[:1] == ['test:surface']:
        return command[1]
    if command[:2] == ['validate', 'surface']:
        return command[2]
    return 'borrower-web'  # Historical direct-package experiment treatment.
