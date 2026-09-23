"""Every failure the slice must distinguish, named once.

A caller that catches `PandoraError` catches everything Pandora raises on
purpose; anything else escaping is a bug. The exit-code mapping lives in
`pandora.exits` so that the client and the CLI agree without importing each
other.
"""


class PandoraError(Exception):
    """Base class for every deliberate failure."""


class ConfigError(PandoraError):
    """A pandora.toml Pandora refuses to load."""


class Refused(PandoraError):
    """An argv a configured job claims but cannot accept.

    Refusal is a pre-submission verdict, so the client may still run locally.
    """


class NotClaimed(PandoraError):
    """No configured job claims this argv. Not an error; the caller runs local."""


class SnapshotError(PandoraError):
    """The worktree could not be frozen (moving tree, secret file, symlink out)."""


class TransferError(PandoraError):
    """The frozen source could not be placed in the worker's source cache."""


class WorkerUnreachable(PandoraError):
    """The worker did not answer. Provably nothing ran: fallback is allowed."""


class EngineError(PandoraError):
    """The worker answered, but the engine refused or failed the request."""


class ExecutionUncertain(PandoraError):
    """The submit call failed and the engine could not be asked what it did.

    Not a `WorkerUnreachable`: that one means provably nothing ran, and earns a
    fallback. This one means the engine may have claimed the request and started
    the command before the reply was lost, so running it here as well could run
    it twice. Deliberately outside both fallback classes, so no `except` written
    for them can catch it by accident.
    """


class StaleRun(EngineError):
    """The named run is not in the ledger, or belongs to a different request."""


class ValidationRejected(PandoraError):
    """The repository's own pre-flight validator rejected the argv.

    Carries that validator's exit code and stderr so the client can reproduce
    the repository's own message rather than inventing one.
    """

    def __init__(self, message, code=1, stderr=''):
        self.code = code
        self.stderr = stderr
        super().__init__(message)
