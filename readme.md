```
# POSIX Shell
curl -fsSL https://raw.githubusercontent.com/V-Sekai-fire/contract-bootstrap/main/main/bootstrap.sh | sh
# Windows Powershell
irm https://raw.githubusercontent.com/V-Sekai-fire/contract-bootstrap/main/main/bootstrap.ps1 | iex
```

Run on a bare machine to get a synced, tooled workspace. The pixi environment,
the bootstrap and install scripts, and their pins live here, separated from the
manifest repository, which places this repo and linkfiles these files to the
workspace root.

## Offline: a mirror of exactly what the locks pin

`pixi_mirror.py` copies the artifacts a `pixi.lock` pins for one platform, each checked against the
lock's sha256, so a machine with no network installs the same environment. On a connected machine:

    python3 pixi_mirror.py build --lock pixi.lock --platform linux-64 --out MIRROR

Then, offline, with MIRROR copied over:

    python3 pixi_mirror.py verify --lock pixi.lock --platform linux-64 --out MIRROR
    python3 pixi_mirror.py serve --out MIRROR &
    python3 pixi_mirror.py config --lock pixi.lock --platform linux-64 > .pixi/config.toml
    pixi install --locked --all

pixi refuses `file://` mirrors, so `serve` puts the tree on loopback. Install with `--locked`, not
`--frozen`: `--frozen` installs a lock that no longer matches `pixi.toml` without saying so.
