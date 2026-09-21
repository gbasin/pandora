# Transcript mining, 2026-09-20

Read-only scripts written by two analysis agents to size post-red agent waste.
They parse local Claude Code and Codex JSONL transcripts. Parsed data is not
committed because it contains session content. Paths are hard-coded to one
machine. Findings and limits are in
[the evidence note](../../notes/trough-work-evidence-2026-09-20.md).

- `eichler/`: ordinary eichler sessions. The keyword classifier was wrong 61% of
  the time against a 66-episode manual read; do not reuse it without that step.
- `trials/`: Pandora trial sessions with evaluator-seeded faults.
