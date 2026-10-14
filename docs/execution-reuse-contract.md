# Shared execution reuse contract

`pandora.client.execution_reuse` implements pure reuse decisions. The
`pandora-execution-reuse` action authenticates immutable receipts and produces
an exclusion plan. Repository adapters collect the requested units and execute
that plan. A family is enabled only when its reviewed producer, runtime, policy,
and consumer are installed together. Missing measurements or evidence run the
original selection.

The caller authenticates the signed envelope and verifies its report digest
before passing evidence to `evaluate`. It constructs evidence from authoritative
signed envelope context plus the digest-bound report. Repository, source tree,
engine identity and completion time come from the engine envelope. Producer
report fields cannot override them. The caller rejects conflicting values. The caller reads policy
from trusted main, independently of the candidate branch. The function does not
verify signatures or establish that its arguments are trusted.

## Schema 1

Evidence and request name `repo`, `family`, `check_id`, `source_tree`,
`engine_identity`, `runtime`, `selection`, and `units`. Policy names the same
repository, family and check. Policy must explicitly set `enabled: true`;
missing or disabled policies grant no reuse. The approved engine identity is a measured SHA-256
bundle identity. Evidence, request and trusted policy must agree on runtime.
Evidence and request must agree on engine identity, and policy must approve it.
Unit IDs express the requested execution scope. `selection` records semantic
selection context and must match exactly, including name, line or tag filters.
Independent-unit policy permits different unit sets, not different filters.
Runtime objects compare as exact canonical JSON. There is no implicit OS,
worker-count, toolchain, environment or service equivalence.

An adapter includes every relevant execution setting in `runtime` and
`selection`. Examples include runtime and browser versions, native SDK and
architecture, fixture/service identities, environment digests, configuration,
and execution flags. A profile field that claims portability does not grant
permission to normalize differences. The trusted producer and policy must
establish what the measured profile means. Missing measurements prevent rollout
for that adapter; the generic matcher cannot infer omitted dependencies.

Evidence additionally contains `finished` (Unix seconds), `complete: true`,
`outcome: "passed"`, `inputs`, and `input_manifest`. Policy requires a positive
integer `max_age_seconds`. Future and expired evidence cannot authorize reuse.

Request units are objects with stable `id` values. Evidence units also require
`complete: true`, `outcome: "passed"`, and exactly one attempt:
`{"ordinal": 0, "outcome": "passed"}`. Retried, skipped, expected-failure and
incomplete units cannot authorize reuse. A unit is a complete adapter-defined
boundary, such as a whole test file, journey, arc, build or static invocation.
Adapters must prove completion of that boundary; individual cases cannot be
credited as a complete file.

Policy defaults to `granularity: "invocation"`. Evidence must then cover the
exact requested unit set, with every unit reusable. A reviewed policy can
explicitly select `"independent-unit"`; evidence may then cover a subset or
superset, and complete matching requested units can be credited separately.
Independence is a reviewed property of the adapter and execution environment,
not inferred from identifiers. Never combine incomplete case reports into a
complete unit.

## Source inputs

Policy `inputs` is an object with `root` (boolean), `paths` (file paths), and
`directories` (recursive directory paths). `root: true` selects the full Git
tree. Scoped policies must review the complete input closure. They include the
producer, adapter, configuration, lockfiles, tests, application dependencies,
fixtures and generated inputs that affect the check. Policy `required_paths`
explicitly names producer and configuration paths; each must fall within the
input scope. This structural check does not replace review of completeness.

`input_manifest(worktree, tree, spec)` reads immutable Git tree objects. It
records missing paths, recursive directory membership, file modes, blob object
identities. Source identities must name Git tree objects, not commits. A file
input naming a directory, or a directory input naming a file or gitlink, fails
closed. A missing path remains an explicit input. Added or removed files in a selected directory
change its manifest. Gitlinks fail closed because the repository tree does not
contain their source contents. This first version also rejects selected
symlinks, including symlink ancestors of selected paths. Symlink targets can
introduce inputs outside the reviewed closure. A future adapter must prove
target closure before authorizing these inputs. Full root policies on trees
containing symlinks therefore produce no reuse candidates. Unsafe paths fail closed. Files ignored by Git,
untracked generated inputs and external state require separate measured runtime
identities or an adapter that records them before reuse is enabled.

Evidence binds the exact input specification and manifest digest in its signed
report. The matcher recomputes the digest from the evidence tree and separately
from the requested tree. Both must match. An unrelated tree change can therefore
reuse scoped evidence without renewing hardcoded object allowlists. Full root
closure naturally requires the same tracked contents.

## Decisions

`evaluate(evidence, request, policy, manifest=reader, now=seconds)` accepts a
trusted immutable-tree reader. A caller can bind `input_manifest` to the
repository with `functools.partial`. The reader returns a SHA-256 manifest digest.

Results separate diagnostics from execution authority. `eligible_units` lists
complete matching units. `remaining_units` lists requested units that need
execution. `whole_complete` reports whether all requested units match.
`skip_units` and `reusable` are the execution candidates and permission signal.
Any malformed or incompatible context returns no skip candidates.
When request units are valid, failed reuse checks preserve every requested unit
in `remaining_units`. If the request units themselves are malformed, the caller
must use its original execution plan; an empty result is not proof that no work
is required.

Both override arguments require actual booleans. `canary=True` or `force_full=True` retains eligibility diagnostics but returns
no skip candidates. `remaining_units` then contains every requested unit,
because all require execution even when eligibility diagnostics match. Full canaries must bypass all reuse wiring. Merge queue
callers collect the requested execution for the actual queue tree and apply the
same input comparison. No PR-head shortcut authorizes a different queue tree.

## Engine inventory transport

New engines capture `input_inventory` immediately after synthetic Git creation,
before grafting build files, preparing source, or executing repository commands.
The signed envelope binds `{v: 1, source_tree, entries, sha256}`. Entries contain
Git modes, object types, and object IDs for every recursive tree member. The
digest is SHA-256 over canonical entries JSON. Capture failure preserves the
run but provides no inventory authority. Older engines omit this field.

After signature verification, CI may call
`attested_manifest(envelope.input_inventory, envelope.tree, policy.inputs)`
when synthetic source objects are unavailable. The request side always reads
its actual immutable CI tree. This replaces unavailable source object transport;
it does not permit a producer report to assert its own input manifest.

An adapter can declare artifact `tmp/pandora-execution-report.json`. The engine
sets `PANDORA_EXECUTION_REPORT` and `PANDORA_EXECUTION_RUN`. Reports use
`kind: "pandora-execution-report"`, `schema: 1`, the exact `worker_run`, and an
`execution` object with complete passed execution evidence. The engine binds
exact report bytes through the existing `test_evidence` digest. Structural
collection is not proof that the adapter is trustworthy. CI must authenticate
the envelope and separately validate the adapter, complete unit boundaries,
and runtime against trusted policy. Engine `golden`, `golden_pins`, `env_digest`,
`argv`, and `cwd` must match reviewed execution context. Runtime assertions in
a repository report alone never establish equivalence.

Generic consumers also require signed `source_integrity: true`. The engine
checks initial source bytes and modes before and after execution. Materialized
LFS contents must match the SHA-256 and size in the original pointer. Changed
source, unsupported checks, or failed measurements disable reuse. This check
does not attest ignored generated inputs or prevent transient modifications
that are restored before the final measurement. Reviewed preparation and
adapter behavior remain part of the trusted input and runtime closure.


## CI plan integration

The action takes `collection`, `output`, and `force_full`. Collection uses the
schema 1 request described above. CI sets `engine_identity` to null when it has
not executed the worker engine. After authentication and policy approval, the
verifier supplies that receipt's engine identity only to the evaluator's
producer identity comparison. The saved request remains unchanged.

The default branch provides `.github/pandora/allowed_signers` and
`.github/pandora/execution-reuse.json`. The latter has a `checks` object keyed
by `check_id`. Each entry contains the evaluator policy and these transport
constraints:

* `worker_job` names the worker receipt namespace. It defaults to `check_id`.
  Alternatively `worker_jobs` maps each approved job to an object containing
  its own `command` policy. Other context and runtime constraints remain shared.
* `execution_context` contains exact signed `golden`, `golden_pins`,
  `env_digest`, and `cwd` values.
* `command` is an exact argv list, or an object with `prefix`, `suffix`, and
  `unit_arguments: true`. Variable arguments must be literal source paths
  matching complete report units. Optional trusted `unit_path_prefix` maps
  app-relative arguments to repository paths. Flags, globs, traversal, missing
  files, and paths outside the input closure grant no reuse. A reviewed
  `literal_arguments: true` variant permits safe selector tokens for a protected
  wrapper that independently proves complete units. Its fixed prefix includes
  the family and app. Tokens cannot contain flags, patterns, traversal, spaces,
  or control characters. Optional `allowed_flags` permits only exact reviewed
  flag tokens, such as `--runTestsByPath`. Unknown flags remain ineligible.
  This grants no permission to change semantic filters.
* `trusted_adapter_paths` names files or complete directories containing all
  proof code, including reporters, collectors, consumers, runtime verification,
  and their imports. Their signed source entries and CI tree entries must both
  match fetched default-branch entries. Adapter changes run the full selection
  until reviewed and merged. Tests and application code can still vary within
  the reviewed input closure.

A policy may use `runtime_basis` instead of fixed `runtime`. It approves exact
fixed dimensions and lists every remaining dimension in
`runtime_variable_keys`. Unknown dimensions fail closed. Variable measurements,
such as resolved configuration hashes, must still match exactly between the
request and signed report. Their measuring code belongs to the protected proof
code and their configuration inputs belong to the reviewed source closure.
The pure evaluator retains its exact runtime comparison.

The plan contains `request`, `skip_units`, `remaining_units`, and `reusable`.
The consumer must independently collect its original selection and validate
that the plan request and partition match it. A missing or malformed plan runs
the original selection. An empty remainder without a valid request never
means that no work remains.

`coverage` contains digest-bound report coverage only for authorized skipped
units. A complete unit's coverage comes from one receipt. The verifier never
combines incomplete coverage fragments. Journey consumers validate that
coverage together with their executed remainder before global fan-in.

Lookup filters exact approved job namespaces before applying its cap and
prioritizes receipts for the requested CI tree.

Only pull request and merge queue events permit reuse. Every tenth workflow
run and explicit `force_full` execute the original selection. Canary plans
contain no skip authority or reused coverage. They retain authenticated
eligibility diagnostics for comparison with the full execution. Merge queue requests bind the
actual checked-out queue tree.

Preparation receives no report path or run identity. After preparation, the
engine removes any prior generic report before executing the command. Cleanup
or source integrity failures preserve test execution and suppress generic
report authorization. Payloads are bounded at 8 MiB and reports at 4 MiB;
bounded lookup considers at most 128 immutable receipts. Truncation can miss a
reuse opportunity but cannot authorize extra work to be skipped.


Scoped inputs may opt into `bind_root_entries: true`. This adds the root-level
names, modes, and object types to the manifest without adding excluded file
contents. A new automatic configuration file or a changed root entry invalidates
reuse. Editing an excluded existing documentation file does not. Existing
input specifications retain their original behavior.

Instead of fixed golden values, a policy may supply `execution_context_basis`
with `image` (an approved resolved Incus fingerprint), `lockfiles` (the exact
recognized root lockfile set), `env_digest`, and `cwd`. The verifier reads the
worker recipe from trusted main and hashes immutable current lockfile blobs to
reconstruct the same toolchain fingerprint and golden pins as the engine.
`pandora.toml` must belong to protected proof code. Every lockfile must be a
regular file bound by the input scope. Changed dependency locks invalidate old
receipts but a matching new receipt needs no policy hash renewal. New image pins
and worker recipe changes still require review on the default branch.
