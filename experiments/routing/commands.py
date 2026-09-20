"""Bounded argv recognition; never parse or rewrite arbitrary shell text."""
import shlex


def classify(argv, treatment='normal'):
    command = argv[1:] if argv[:1] == ['run'] else argv
    journey = command[1:] if command[:1] == ['validate'] else command
    if journey[:1] in (['journey'], ['journeys']):
        if journey in (['journey', 'S0-01'], ['journey', 'S0-01', '--update']):
            return 'journey', journey[1:], ''
        return 'reject', [], 'This trial supports pnpm journey S0-01 [--update] only. No validation started.'
    if command[:2] == ['test:surface', 'borrower-web']:
        selectors = command[2:]
    elif command[:3] == ['validate', 'surface', 'borrower-web']:
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
    if any(x.startswith('-') for x in selectors):
        return 'reject', [], 'This trial supports file selectors only. No validation started.'
    return 'remote', selectors, ''
