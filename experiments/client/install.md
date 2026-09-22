# Install the Pandora client daemon and pnpm shim

This is a procedure. Do the steps in order. No step in this file was executed by
the POC. Read `notes/client-daemon-poc-2026-09-21.md` before you start.

Every step is reversible. Step 9 reverses all of them.

## Before you start

You need three facts about this machine.

1. The shim must win the `pnpm` name in every shell an agent uses.
2. On this Mac, `~/.zshenv` prepends `/opt/homebrew/bin` to `PATH` for every
   zsh, including nested non-interactive ones.
3. Because of fact 2, a shim in `~/.local/bin` wins in a terminal and in an
   Agentboard pane, and loses in the Claude Code bash tool and in Codex lanes.

Choose one install location now.

* Choose **A** if you accept one edit to `~/.zshenv`. A wins everywhere.
* Choose **B** if you do not want a dotfile edit. B wins in a terminal and in
  Agentboard panes only.

## 1. Create the state directory

```sh
mkdir -p ~/.local/state/pandora/default
chmod 700 ~/.local/state/pandora
chmod 700 ~/.local/state/pandora/default
```

The mode must be 0700. The daemon serves only your own uid, and the directory
mode is the outer control.

## 2. Write the daemon configuration

```sh
cat > ~/.local/state/pandora/default/config.json <<'JSON'
{
  "backend": {"mode": "ok"},
  "fallback_slots": 2,
  "fallback_wait_seconds": 0,
  "require_token": false
}
JSON
```

Set `fallback_slots` to the number of heavy suites this Mac can run at once.
Set `fallback_wait_seconds` to 0 to refuse extra fallbacks. Set it to a number
of seconds to make them queue instead.

## 3. Start the daemon once, by hand

```sh
python3 -B <repo>/experiments/client/daemon.py --state ~/.local/state/pandora/default
```

Leave it running. Open a second terminal for the next steps.

## 4. Confirm the daemon answers

```sh
python3 -B <repo>/experiments/client/cli.py --state ~/.local/state/pandora/default ping
```

The output must contain `"t": "pong"`. Stop here if it does not.

## 5. Install the shim

### Option A: win in every shell

Copy the shim into `~/.local/bin`.

```sh
mkdir -p ~/.local/bin
cp <repo>/experiments/client/bin/pnpm ~/.local/bin/pnpm
chmod 755 ~/.local/bin/pnpm
```

Tell the shim where its Python half lives.

```sh
printf 'export PANDORA_CLIENT_DIR=%s\n' "<repo>/experiments/client" >> ~/.zshenv
```

Add the PATH line to `~/.zshenv`, after the Homebrew block.

```sh
printf 'export PATH="$HOME/.local/bin:$PATH"\n' >> ~/.zshenv
```

Open a new shell. Verify the shim wins.

```sh
/bin/zsh -lc 'command -v pnpm'
```

The output must be `~/.local/bin/pnpm`.

### Option B: no dotfile edit

Copy the shim into `~/.local/bin` as in option A. Do not edit `~/.zshenv`.
Accept that the Claude Code bash tool and Codex lanes keep using Homebrew's
pnpm, so commands typed there are never routed.

## 6. Enrol one repository

```sh
python3 -B <repo>/experiments/client/cli.py \
  --state ~/.local/state/pandora/default \
  enrol /Users/YOU/Code/eichler --name eichler \
  --config /Users/YOU/Code/eichler/pandora.toml
```

This writes one file at `<git common dir>/pandora-enrolled`. That single file
covers every worktree of the repository, including worktrees created later.

Check it.

```sh
cat "$(git -C /Users/YOU/Code/eichler rev-parse --git-common-dir)/pandora-enrolled"
```

## 7. Verify routing end to end

Run an unclaimed command in the repository. It must behave exactly as before.

```sh
cd /Users/YOU/Code/eichler && pnpm --version
```

Run a claimed command. It must reach the daemon.

```sh
cd /Users/YOU/Code/eichler && pnpm test:unit
```

Set `PANDORA_OFF=1` to force any command local.

```sh
PANDORA_OFF=1 pnpm test:unit
```

## 8. Install the launchd agent

Copy the sample plist.

```sh
cp <repo>/experiments/client/launchd/me.pandora.clientd.plist \
   ~/Library/LaunchAgents/me.pandora.clientd.plist
```

Replace `REPLACE_ME` with your username in that file. Replace
`/ABSOLUTE/PATH/TO` with the repository path.

Stop the hand-started daemon from step 3 first. Two daemons cannot share one
state directory; the second one exits with `already running`.

Load the agent.

```sh
launchctl load ~/Library/LaunchAgents/me.pandora.clientd.plist
```

Confirm it is running.

```sh
launchctl list | grep me.pandora.clientd
python3 -B <repo>/experiments/client/cli.py --state ~/.local/state/pandora/default ping
```

## 9. Uninstall

Unload the agent.

```sh
launchctl unload ~/Library/LaunchAgents/me.pandora.clientd.plist
rm ~/Library/LaunchAgents/me.pandora.clientd.plist
```

Remove the shim.

```sh
rm ~/.local/bin/pnpm
```

Remove the two lines you added to `~/.zshenv`, if you chose option A.

Un-enrol every repository.

```sh
python3 -B <repo>/experiments/client/cli.py \
  --state ~/.local/state/pandora/default unenrol /Users/YOU/Code/eichler
```

Delete the state directory.

```sh
rm -rf ~/.local/state/pandora/default
```

## Operating notes

Read what still runs locally.

```sh
python3 -B <repo>/experiments/client/cli.py --state ~/.local/state/pandora/default stats
```

Re-attach to a run you disconnected from.

```sh
python3 -B <repo>/experiments/client/cli.py --state ~/.local/state/pandora/default wait <run-id>
```

Turn routing off for one command.

```sh
PANDORA_OFF=1 pnpm journeys
```

Turn routing off for a whole shell.

```sh
export PANDORA_OFF=1
```
