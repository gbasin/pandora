"""provision.sh runs from the worker's stdin, so no Incus create may read it."""
import re
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'worker' / 'provision.sh'
CREATE = re.compile(r'\$[IP] (?:admin init|(?:storage|network|project|profile)(?: volume)? create)\b')


class ProvisionScriptTest(unittest.TestCase):
    def test_every_incus_create_has_stdin_closed(self):
        offenders = []
        for number, line in enumerate(SCRIPT.read_text().splitlines(), 1):
            if CREATE.search(line) and '</dev/null' not in line:
                offenders.append('%d: %s' % (number, line.strip()))
        self.assertEqual(offenders, [], 'incus create without </dev/null reads the script as YAML')


if __name__ == '__main__':
    unittest.main()
