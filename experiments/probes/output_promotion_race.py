#!/usr/bin/env python3
"""Show why compare-then-replace cannot protect concurrent workspace edits.

This deliberately flawed candidate protocol is not Pandora's deployed behavior.
All operations use a disposable directory. Successful reproduction exits zero.
"""
import hashlib
import json
import os
import tempfile
from pathlib import Path

with tempfile.TemporaryDirectory(prefix='pandora-output-race-') as directory:
    destination = Path(directory) / 'output.js'
    incoming = Path(directory) / 'incoming.js'
    destination.write_text('original output\n')
    baseline = hashlib.sha256(destination.read_bytes()).hexdigest()
    incoming.write_text('remote build output\n')
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == baseline
    # Deterministically schedule an editor after comparison, before replacement.
    destination.write_text('concurrent local edit\n')
    os.replace(incoming, destination)
    lost = destination.read_text() != 'concurrent local edit\n'
    assert lost
    print(json.dumps({'candidate': 'compare-then-replace',
                      'concurrent_edit_lost': lost,
                      'deployed_workspace_writeback_tested': False}, indent=2))
