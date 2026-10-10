# contract-bootstrap

One step from a bare machine to a synced, tooled workspace.

## What it is for

It holds the workspace's pixi environment, the bootstrap and install scripts, and the pins they install, apart from the goal manifest that places this repository and links these files to the workspace root. A mirror script copies exactly what a lock pins, checked against the lock's hashes, so a machine with no network installs the same environment.

## Build and run

In a POSIX shell:

    curl -fsSL https://raw.githubusercontent.com/V-Sekai-fire/contract-bootstrap/main/bootstrap.sh | sh

In PowerShell:

    irm https://raw.githubusercontent.com/V-Sekai-fire/contract-bootstrap/main/bootstrap.ps1 | iex

When it finishes, a new agent starts at the "Start here" paragraph of the workspace's `AGENTS.md`, which leads to RFD 2294. The mirror script's own help gives the offline steps.

## Licence

MIT. See [LICENSE](LICENSE).
