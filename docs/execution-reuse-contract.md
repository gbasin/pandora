# Shared execution reuse contract

`pandora.client.execution_reuse` is a prerequisite for broader reuse. It does
not publish new receipts or skip CI checks. Existing unit and suite protocols
remain unchanged. Unit, surface, journey, arc, Node, native, and static adapters
can use this contract once their execution evidence meets its requirements.
These families are contract vocabulary, not operational integrations. Existing
receipts do not carry this new input specification and manifest binding.
Synthetic source Git objects can also be absent from GitHub. Missing objects
or manifest bindings must fail closed. Engine-attested complete input manifests
and reviewed producer adapters are required before actual rollout.

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
