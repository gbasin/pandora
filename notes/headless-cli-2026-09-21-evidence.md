---
status: log
---

# Headless Claude foreground evidence

On 2026-09-21, controller run `pandora-headless-cli-20260920`, lane
`opus-long-probe`, invoked the evaluator launcher after commit `e5e73f5`.

The launcher scopes `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` to its
`claude -p` child, disallows `Monitor`, and leaves interactive Claude
unchanged. Anthropic documents Claude Code environment variables in
[its settings reference](https://code.claude.com/docs/en/settings#environment-variables).
The headless lifecycle failure is tracked in
[anthropics/claude-code#81476](https://github.com/anthropics/claude-code/issues/81476)
and [#89495](https://github.com/anthropics/claude-code/issues/89495).

The probe ran foreground Bash:

```
python3 -c 'import time; time.sleep(130); print("PANDORA_LONG_COMMAND_COMPLETED")'
```

The transcript recorded 30, 60, 90, and 120 second heartbeats, then delivered
`PANDORA_LONG_COMMAND_COMPLETED` to the same tool call. The final assistant
response followed that output and reported normal completion. The tool list did
not include `Monitor`. No files changed and no remote validation started.

This is evidence for the evaluator-only launcher setting, not an interactive
Claude configuration change.

