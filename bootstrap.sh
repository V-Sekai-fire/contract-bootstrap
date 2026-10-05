#!/bin/sh
# One step from a bare machine to a synced, tooled workspace, on Linux and macOS:
#
#   curl -fsSL https://raw.githubusercontent.com/V-Sekai-fire/contract-bootstrap/main/bootstrap.sh | sh
#
# Runs in the current directory, which becomes the repo client root.
set -eu

raw=${WEFTSPUN_RAW:-https://raw.githubusercontent.com/V-Sekai-fire/contract-bootstrap/main}
manifest=${WEFTSPUN_MANIFEST:-https://github.com/V-Sekai-fire/contract-manifest-taskweft.git}
branch=${WEFTSPUN_BRANCH:-main}
bin="${LOCAL_BIN:-$HOME/.local/bin}"
pixi_bin="${PIXI_HOME:-$HOME/.pixi}/bin"
# Where the manifest places the contract-bootstrap project.
boot=2-contract/bootstrap

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

sha_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

# 1. The pins, over the CDN, which is the one fetch nothing on disk can vouch for yet.
curl -fsSL -o "$work/pins" "$raw/bootstrap-pins.txt"
repo_source=$(awk '$1=="repo" && $2=="source"{print $3}' "$work/pins")
repo_sha=$(awk '$1=="repo" && $2=="sha256"{print $3}' "$work/pins")

# 2. The pinned repo launcher.
curl -fsSL -o "$work/repo" "$repo_source"
got=$(sha_of "$work/repo")
[ "$got" = "$repo_sha" ] || { echo "checksum mismatch for the repo launcher: got $got, pinned $repo_sha" >&2; exit 1; }
mkdir -p "$bin"
install -m 0755 "$work/repo" "$bin/repo"
PATH="$bin:$PATH"
export PATH

# 3. The manifest and this bootstrap project, over git, which is what makes the pins
#    trustworthy. The heavy Hugging Face projects are git-lfs, and repo leaves LFS
#    content as pointer files unless --git-lfs asked for it, so the default sync is
#    metadata only; set WEFTSPUN_GIT_LFS=1 to pull the blobs too, tens of gigabytes.
repo init ${WEFTSPUN_GIT_LFS:+--git-lfs} -u "$manifest" -b "$branch"
repo sync "$boot"

# 4. The CDN copy against the git copy. A difference means the pins that chose the
#    launcher in step 2 were not the pins this repository holds.
if ! cmp -s "$work/pins" "$boot/bootstrap-pins.txt"; then
  echo "the pins served by $raw differ from the ones in $boot" >&2
  exit 1
fi

# 5. pixi, from the pins now on disk, then the whole workspace.
sh "$boot/install.sh"
repo sync
PATH="$pixi_bin:$PATH"
export PATH
pixi install --manifest-path "$boot/pixi.toml" --all

# 6. Each project's prek gates as its pre-push hook, so a push runs what CI runs.
#    Git exports GIT_DIR to hooks in a worktree, and a gate self-test that builds a scratch
#    repository with `git -C` then commits into the repository being pushed, so the hook
#    unsets it first.
if command -v prek >/dev/null 2>&1; then
  repo forall -c '
    test -f .pre-commit-config.yaml || exit 0
    prek install -t pre-push || exit 1
    h="$(cd "$(git rev-parse --git-common-dir)" && pwd)/hooks/pre-push"
    grep -q "^unset GIT_DIR" "$h" && exit 0
    awk "/^HERE=/ { print \"unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_PREFIX\" } { print }" \
      "$h" > "$h.tmp" && cat "$h.tmp" > "$h" && rm -f "$h.tmp"
    grep -q "^unset GIT_DIR" "$h" || echo "$REPO_PATH: pre-push hook left without the GIT_DIR guard" >&2
  '
else
  echo "prek is not on PATH; pushes will not run the CI gates locally" >&2
fi

echo
echo "Workspace ready. Add these to PATH: $bin $pixi_bin"
echo
echo 'Agent, start here: read the "Start here" paragraph at the top of AGENTS.md, then open'
echo '2-contract/manuals-weftspun/rfd/2294-agent-knowledge-lives-in-rfds-not-in-desk-memory.exs,'
echo 'find: details "Starting out in the workspace", and do its numbered steps in order, one at a time.'
echo 'Read a file before you edit it. Never force-push, delete, or use --no-verify.'
echo 'No GitHub credentials? Stop after committing and tell the operator your branch name.'
echo 'If a step fails or you are unsure, stop and ask the operator.'
