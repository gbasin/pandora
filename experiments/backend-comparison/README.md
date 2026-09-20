# Backend comparison evidence

Read [the dated findings](../../notes/backend-comparison-2026-09-20.md) for the
native BuildKit and Mutagen comparison, timing boundaries, and limitations.

The files under `evidence/2026-09-20/scripts` are archived operator POCs. They use
fixed trial paths and are not supported command adapters. Do not execute them
against an existing workspace without adapting their paths and lifecycle.
The native script expects `server.py` on the worker and a private Buildx config.
The Mutagen scripts expect its v0.18.1 binary and a dedicated daemon data directory.
Temporary binaries, source copies, daemon state, registry storage, and mirrors
were removed after the run. Log trailing whitespace was normalized when archived.
