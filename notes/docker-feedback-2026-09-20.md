---
status: log
---
# Docker source and output feedback, 2026-09-20

The contention trial exposed two misunderstandings. One Opus agent inferred that
an unmounted Docker run might inject current local source, making a rebuild
optional. Several agents expected a bind mount to retrieve generated files.
The successful runs and artifacts did not make those assumptions correct.

Image-only requests now say that they use the built image and that source edits
require a rebuild. Empty request staging no longer prints a frozen-source identity
or claims to transfer changed source. The worker's common waiting message says
input verification completed, without implying that an image-only run captured
local source.

For a profile with declared outputs, Docker run feedback prints each container
path and its local destination. It explains that outputs return after success
without an output-directory mount, and gives the ordinary image-only run form.
This appears before command classification so a rejected output-mount attempt
receives the same guidance. Unsupported commands still reject without submitting
work. Snapshot, image resolution, artifact publication, and queue semantics are
unchanged.

Validation included 13 routing tests and 18 worker tests. A real compiled-image
run verified the expected source marker and returned its generated outputs.
A rejected output mount returned 64 with the new hint and created no active
request. The final worker wording was also exercised on the VM. Logs and receipts
are retained under `experiments/contention/evidence/2026-09-20/feedback`.

This is a wording correction supported by the preceding agent transcripts.
No new coding-agent trial was run after the correction, so improved agent
understanding remains to be measured. The observed 280-second queue wait remains
an independent FIFO-admission issue.
