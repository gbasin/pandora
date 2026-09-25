# Working in this repository

Read `README.md` first. It says what Pandora does and how.

* `~/Code/pandora` on the owner's Mac is the checkout the daemon was installed
  from. Never edit it in place. Work in a worktree under `~/Code/pandora-wt/`.
* Do not run `pandora daemon`, `pandora upgrade`, or `pandora enroll`, and do
  not touch `~/.config/pandora` or `~/.local/state/pandora`. Those change the
  live daemon. The owner runs them.
* Tests: `python3 -m unittest discover -s pandora`. Lint:
  `ruff check --select F,E9 . --exclude experiments`, the same check CI runs.
  Shims: `shellcheck bin/pnpm bin/pandora`. Run the suite once per change.
* `pandora --help` is the contract. When docs and help disagree, fix both in the
  same change. `docs/agents-paragraphs.md` is what consuming repositories copy.
* Dated notes under `notes/` are logs. Do not edit them. Write a new one.
* American English. No em dashes.
