# contract-bootstrap

One step from a bare machine to a synced, tooled workspace.

## What it is for

It holds the workspace's pixi environment, the bootstrap and install scripts, and the pins they install, apart from the goal manifest that places this repository and links these files to the workspace root. A mirror script copies exactly what a lock pins, checked against the lock's hashes, so a machine with no network installs the same environment.

## Build and run

In a POSIX shell:

    curl -fsSL https://raw.githubusercontent.com/V-Sekai-fire/contract-bootstrap/main/bootstrap.sh | sh

In PowerShell:

    irm https://raw.githubusercontent.com/V-Sekai-fire/contract-bootstrap/main/bootstrap.ps1 | iex

For a machine with no network, build the mirror with `pixi_mirror.py build` on a connected machine and copy it over; its own help lists the subcommands. Offline, after `verify` and `serve`, write the mirror's settings into the pixi config and install with `--locked`, never `--frozen`, which installs a lock that no longer matches `pixi.toml` without saying so:

    python3 pixi_mirror.py config --lock pixi.lock --platform linux-64 > .pixi/config.toml
    pixi install --locked --all

When the bootstrap finishes, a new agent, a small local model included, is given this:

```
Agent, start here: read the "Start here" paragraph at the top of AGENTS.md, then open
2-contract/manuals-weftspun/rfd/2294-agent-knowledge-lives-in-rfds-not-in-desk-memory.exs,
find: details "Starting out in the workspace", and do its numbered steps in order, one at a time.
Read a file before you edit it. Never force-push, delete, or use --no-verify.
No GitHub credentials? Stop after committing and tell the operator your branch name.
If a step fails or you are unsure, stop and ask the operator.
```

## Licence

There is no LICENSE file. The gate script's SPDX header marks it Apache-2.0 OR MIT.
