# Image collection during metadata upload, 2026-09-20

The explicit-wait twelve-agent trial exposed two pre-test infrastructure failures.
`image_gc.protected_images` read another attempt's `submission.json` while scp
had opened but not completed it. JSON decoding failed during worker preparation.
The affected requests returned exit 70 and verified cleanup; agents retried them.
No tests executed for those two requests.

Metadata now transfers through rsync with delayed per-file rename. Image collection
also defers without Docker inventory, deletion, or pin removal when its protection
metadata is incomplete or malformed. It retries on a later ordinary collection.
This conservative path covers older uploaders and leaves corrupt records visible
for operator investigation rather than guessing which images are safe to delete.

The process/thread regression holds a metadata file empty while collection runs,
then completes a reference to an eligible image. Collection first defers, then
preserves that protected image. Additional cases preserve acknowledged pins when
a later protection record is malformed. The assembled code passed 236 worker and
87 routing tests.

A real-Docker probe on the authorized VM used an isolated registry root:
`/home/ubuntu/pandora-gc-transfer-proof/5aa2482270254a28bbdb72c8ea6e90b7`.
It created only a disposable alias,
`pandora-build:b9a828f0d69847888cc3ef1d66852bc3`, of an existing dependency image.
During an intentionally paused write, collection deferred and the tag survived.
After the write completed, the referenced tag remained protected. After explicit
release and cleanup evidence, collection removed that tag and preserved the original
image. The local receipt is
`~/.local/state/pandora/v01-final-eval/gc-transfer-proof.log`.

A fresh twelve-agent sample evaluates the integrated rsync upload path. Its outcome
belongs in a separate dated record; this probe alone is not an agent-capacity claim.
