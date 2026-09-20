# Scoped Docker pilot

The launcher wraps `docker` only inside the opted-in process tree. It parses argv
and routes the following forms through the same SSH snapshot, queue, recovery,
and output-delivery path as validation workflows:

```sh
docker build [-f RELATIVE_DOCKERFILE] -t TAG .
docker run --rm [-v "$PWD:DECLARED_PATH[:ro]"] TAG [COMMAND [ARG...]]
docker image rm TAG
```

Start the session with an external JSON profile:

```sh
python3 /path/to/pandora/experiments/routing/launch.py \
  --host ubuntu@WORKER_IP \
  --state "$HOME/.local/state/pandora/pilot" \
  --docker-profile /path/to/profile.json \
  -- codex
```

Use `-- claude` for Claude. The profile is read by the launcher and passed through
the session environment. Agents need no permission to read its original location.
Do not put secrets in the profile. `profile.json` in this directory is the tested
fixture profile:

```json
{
  "dockerfiles": ["Dockerfile"],
  "mounts": ["/workspace"],
  "outputs": [{"container": "/workspace/dist", "workspace": "dist"}],
  "network": "none"
}
```

Declare only generated output directories that Pandora may own during publication.
Local destinations must be Git-ignored and contain no tracked files. Use an empty
outputs list for commands that produce no workspace output. Network may be `none`
or `bridge`; there are no forwarded ports. Unknown profile fields are rejected.
The same profile applies to every Docker run in that session.

## Images across calls

Logical tags are stored remotely under a hash of the canonical worktree path.
They survive launcher and local state-directory changes on the same client path
and worker. Moving a worktree or switching workers gives it another image namespace.
Two worktrees can use `app:test` independently. An omitted tag means `:latest`.
A failed build preserves the old mapping. A successful build changes it atomically
after BuildKit cleanup. Image removal removes this worktree's mapping only.

Runs pin the image ID before queueing. Physical attempt tags are retained so an
accepted run cannot lose its image after a rebuild or mapping removal. **Physical
image garbage collection is not implemented.** Monitor disk use; the shared worker
refuses new work below 10 GiB free. Do not use Docker's global prune while accepted
requests depend on retained images.

## Source and outputs

Builds capture tracked and nonignored source, with Pandora's existing secret and
dependency exclusions. BuildKit then applies `.dockerignore` and Dockerfile-specific
ignore rules. This is a narrower context than ordinary Docker: Git-ignored inputs,
including old local build outputs, are not sent. A Dockerfile needing those inputs
must build them remotely or wait for a future declared-input extension. Local tags
inside `FROM` are not translated to Pandora mappings. Base images must resolve
through BuildKit's normal registry behavior. No registry credentials, SSH agent,
secret mounts, build args, external contexts, or host Docker socket are forwarded.

A run without `-v` uses image contents. It does not capture or inject local source.
A run with the supported mount receives a private copy of current captured source.
The copy hides image files at that path, including any image-installed dependencies.
Its ownership matches the image user. Pandora resolves named users through the
image's passwd/group files. It preserves the image's configured user and command.
The original snapshot and other worktrees remain immutable. `:ro` prevents writes.

Available declared outputs are retained even after a failing command. Only a
successful run publishes outputs at their declared local paths, using the existing
atomic directory-exchange mechanism. Previous generations remain in the attempt's
publication directory. Missing or unsafe outputs prevent successful delivery.
This is generated-output ownership, not general source merging. Never declare a
mixed source/output directory for automatic publication.

The command prints the logical tag, pinned image, relevant source identity, and
an absolute local Docker report path. Build reports include BuildKit provenance
with resolved base images and build configuration. Container reports include exit
and OOM state. Buildx metadata behavior follows the
[Docker build reference](https://docs.docker.com/reference/cli/docker/buildx/build/).

## Limits and failure behavior

Builds use a dedicated persistent BuildKit cache, capped at two CPUs and 6 GiB
without swap. Its image is digest-pinned. Runs have the same CPU/RAM limit, 512
processes, and no extra Linux capabilities. One shared worker slot covers both
workflows and Docker operations. Build and run deadlines are 15 and 20 minutes.

Client loss preserves the accepted run. Retry the same command to recover it.
Explicit cancellation cleans its container or builder. Systemd also invokes
cleanup after worker death. Missing terminal evidence remains an operator case;
cleanup alone never becomes a passing result. A build can publish its tag just
before worker death prevents terminal publication, so operators must reconcile
that state before rerunning it.

Unsupported commands, including Docker read commands, stop with an example. There
is no local fallback. The wrapper also stops Docker when no profile is selected.
Absolute executable paths and PATH overrides still bypass this opt-in mechanism.
This is not OS enforcement, multi-tenant isolation, or full Docker compatibility.
