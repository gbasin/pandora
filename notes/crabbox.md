# Execution foundations assessed on 2026-09-19

This replaces the earlier v0.2-era comparison. Pandora is a design exploration,
not an implemented alternative. Its MVP contract is in [DESIGN.md](../DESIGN.md).
This assessment uses current documentation, not a live integration test.

## Crabbox

Crabbox has leases (environments), runs (executions), and named repository jobs
(recipes for setup, execution, and cleanup). It supports existing SSH hosts and
multiple provisioned/delegated backends, including non-Linux targets. Earlier
claims that macOS is universally out of scope are obsolete.

Dirty-source sync, remote output, explicit artifact downloads, warm leases,
prepared pools, and retained failed environments provide useful building blocks.
Source/Git semantics vary by mode and provider. Test them against the target's
fingerprinting rather than inferring them from a generic rsync description.

The MVP limitations are specific:

- Static SSH is direct-only, outside the coordinator. It does not turn one host
  into a memory-admitted queue of isolated jobs.
- `attach` views coordinator events, not independent detached execution. It
  cannot resume execution after the originating CLI dies; direct runs have no
  central event stream to follow.
- Losing SSH does not prove the remote process stopped. Busy ownership can remain.
- Explicit downloads are not general conflict-safe automatic source write-back.
- Keeping a failed environment and releasing its compute are different features;
  pause/resume support depends on the provider.

Sources: [jobs](https://crabbox.sh/commands/job.html),
[run](https://crabbox.sh/commands/run.html),
[sync](https://crabbox.sh/features/sync.html),
[static SSH](https://crabbox.sh/providers/ssh.html),
[attach](https://crabbox.sh/commands/attach.html),
[pause](https://github.com/openclaw/crabbox/blob/main/docs/commands/pause.md).

## CI control planes

GitHub Actions queues jobs on owned runners. Dispatch accepts a ref and inputs;
dirty-source transfer still needs a snapshot mechanism. Ephemeral registration
does not wipe the machine: our setup must dispose of the execution environment
and enforce limits. Eichler's existing Linux surface recipe makes this a
plausible first experiment, using a separate experimental workflow.

Buildkite's experimental Preflight snapshots staged, unstaged, and nonignored
untracked changes into a temporary commit/branch without changing the worktree.
It pushes the branch, triggers a pipeline, and watches results. Its watcher can
exit when failure begins, before all jobs terminate; evaluate `--exit-on
build-terminal` for blocking semantics. Cleanup, abnormal CLI exit, source branch
lifetime, and artifact return require live tests. It adds account/setup cost.

Sources: [GitHub dispatch](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow),
[runner lifecycle](https://docs.github.com/en/actions/reference/runners/self-hosted-runners),
[Buildkite Preflight](https://buildkite.com/docs/platform/cli/preflight).

## Selection principle

Implement one backend behind session-scoped routing outside the target repo.
Evaluate source identity, ownership, waiting, cancellation, failed-state
inspection, and results with actual agents. Build another path only when a
measured limitation warrants it. Do not build Pandora's former full architecture
merely to compare it with an existing tool.
