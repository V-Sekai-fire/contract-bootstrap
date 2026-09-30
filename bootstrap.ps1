# One step from a bare machine to a synced, tooled workspace, on Windows:
#
#   irm https://raw.githubusercontent.com/V-Sekai-fire/contract-bootstrap/main/main/bootstrap.ps1 | iex
#
# Runs in the current directory, which becomes the repo client root.
$ErrorActionPreference = 'Stop'

$raw = $(if ($env:WEFTSPUN_RAW) { $env:WEFTSPUN_RAW } else { 'https://raw.githubusercontent.com/V-Sekai-fire/contract-bootstrap/main/main' })
$manifest = $(if ($env:WEFTSPUN_MANIFEST) { $env:WEFTSPUN_MANIFEST } else { 'https://github.com/V-Sekai-fire/contract-manifest-taskweft.git' })
$branch = $(if ($env:WEFTSPUN_BRANCH) { $env:WEFTSPUN_BRANCH } else { 'main/main' })
$bin = $(if ($env:LOCAL_BIN) { $env:LOCAL_BIN } else { Join-Path $HOME '.local\bin' })

$pixiHome = $(if ($env:PIXI_HOME) { $env:PIXI_HOME } else { Join-Path $HOME '.pixi' })
$pixiBin = Join-Path $pixiHome 'bin'
# Where the manifest places the contract-bootstrap project.
$boot = Join-Path (Get-Location) '2-contract\bootstrap'

# The heavy Hugging Face projects are git-lfs. repo leaves LFS content as pointer
# files unless `repo init --git-lfs` asked for it, so the default sync is metadata
# only; set WEFTSPUN_GIT_LFS=1 to pull the blobs too, which is tens of gigabytes.
# Assigned in two statements, not a $() subexpression: that unrolls a one-element
# array back to a bare string, and splatting a string spells it out one character
# per argument -- the same trap the pins parser in install.ps1 documents.
$gitLfs = @()
if ($env:WEFTSPUN_GIT_LFS) { $gitLfs = @('--git-lfs') }

# $ErrorActionPreference does not throw on a child process that exits non-zero, so
# every external call below is checked. Without this a failed installer is silent
# until pixi turns up missing two steps later.
function Invoke-Checked {
    param([string]$What, [scriptblock]$Command)
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "$What failed with exit code $LASTEXITCODE" }
}

$work = Join-Path ([System.IO.Path]::GetTempPath()) ([System.IO.Path]::GetRandomFileName())
New-Item -ItemType Directory -Path $work | Out-Null

try {
    # 1. The pins, over the CDN, which is the one fetch nothing on disk can vouch for yet.
    $pins = Join-Path $work 'pins'
    Invoke-WebRequest -UseBasicParsing -Uri "$raw/bootstrap-pins.txt" -OutFile $pins
    
    $repoSource = ""
    $repoSha = ""
    Get-Content $pins | ForEach-Object {
        $line = [string]$_
        if (-not [string]::IsNullOrWhiteSpace($line)) {
            $parts = $line -split '\s+'
            if ($parts.Count -ge 3 -and $parts[0] -eq 'repo') {
                if ($parts[1] -eq 'source') { $repoSource = [string]$parts[2] }
                if ($parts[1] -eq 'sha256') { $repoSha = ([string]$parts[2]).Trim().ToLowerInvariant() }
            }
        }
    }

    # 2. The pinned repo launcher.
    $staged = Join-Path $work 'repo'
    Invoke-WebRequest -UseBasicParsing -Uri $repoSource -OutFile $staged
    $got = (Get-FileHash -Algorithm SHA256 $staged).Hash.ToLowerInvariant()
    if ($got -ne $repoSha) { 
        throw "checksum mismatch for the repo launcher: got $got, pinned $repoSha" 
    }
    New-Item -ItemType Directory -Path $bin -Force | Out-Null
    
    $repoDest = Join-Path $bin 'repo'
    if (Test-Path $repoDest) {
        Remove-Item -Path $repoDest -Force
    }
    Move-Item -Path $staged -Destination $repoDest -Force
    $env:PATH = "$bin;$env:PATH"

    # 3. The manifest and this bootstrap project, over git, which is what makes the
    #    pins trustworthy.
    # Added --no-repo-verify to bypass Windows GPG keyring errors
    Invoke-Checked 'repo init' { python (Join-Path $bin 'repo') init --repo-url=https://gerrit.googlesource.com/git-repo --no-repo-verify @gitLfs -u $manifest -b $branch }
    Invoke-Checked 'repo sync bootstrap' { python (Join-Path $bin 'repo') sync 2-contract/bootstrap }

    # 4. The CDN copy against the git copy. A difference means the pins that chose the
    #    launcher in step 2 were not the pins this repository holds.
    $manifestPins = Join-Path $boot 'bootstrap-pins.txt'
    if (-not (Test-Path $manifestPins)) {
        throw "bootstrap pins not found at $manifestPins"
    }
    
    #    Compared as text with line endings normalized: git checks the file out with CRLF
    #    wherever core.autocrlf is on, and a byte compare against the CDN's LF copy then
    #    reports a difference that is not one.
    $normalize = { param($path) ((Get-Content -Raw $path) -replace "`r`n", "`n") }
    if ((& $normalize $pins) -ne (& $normalize $manifestPins)) {
        throw "the pins served by $raw differ from the ones in $boot"
    }

    # 5. pixi, from the pins now on disk, then the whole workspace.
    $installScript = Join-Path $boot 'install.ps1'
    Invoke-Checked 'install.ps1' { & powershell -ExecutionPolicy Bypass -File $installScript }

    Invoke-Checked 'repo sync' { python (Join-Path $bin 'repo') sync }
    $env:PATH = "$pixiBin;$env:PATH"
    
    $pixiExe = Join-Path $pixiBin 'pixi.exe'
    $pixiManifest = Join-Path $boot 'pixi.toml'
    Invoke-Checked 'pixi install' { & $pixiExe install --manifest-path $pixiManifest --all }

    # 6. Each project's prek gates as its pre-push hook, so a push runs what CI runs.
    #    Git exports GIT_DIR to hooks in a worktree, and a gate self-test that builds a scratch
    #    repository with `git -C` then commits into the repository being pushed, so the hook
    #    unsets it first. The hook runs under Git's sh on Windows too, so the guard is the same
    #    shell line bootstrap.sh inserts, placed before the hook's HERE= line.
    if (Get-Command prek -ErrorAction SilentlyContinue) {
        $guard = 'unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_PREFIX'
        $paths = python (Join-Path $bin 'repo') list -p
        if ($LASTEXITCODE -ne 0) { throw "repo list failed with exit code $LASTEXITCODE" }
        foreach ($path in $paths) {
            $path = $path.Trim()
            if (-not (Test-Path (Join-Path $path '.pre-commit-config.yaml'))) { continue }
            Push-Location $path
            try {
                Invoke-Checked "prek install in $path" { prek install -t pre-push }
                $hook = Join-Path (Resolve-Path (git rev-parse --git-common-dir).Trim()) 'hooks\pre-push'
                $lines = @(Get-Content $hook)
                if (-not ($lines -match '^unset GIT_DIR')) {
                    $out = foreach ($l in $lines) { if ($l -match '^HERE=') { $guard }; $l }
                    # LF and no BOM: sh reads this file.
                    [System.IO.File]::WriteAllText($hook, (($out -join "`n") + "`n"))
                }
                if (-not (@(Get-Content $hook) -match '^unset GIT_DIR')) {
                    Write-Warning "${path}: pre-push hook left without the GIT_DIR guard"
                }
            }
            finally { Pop-Location }
        }
    }
    else {
        Write-Warning 'prek is not on PATH; pushes will not run the CI gates locally'
    }

    Write-Output ''
    Write-Output "Workspace ready. Add these to PATH: $bin $pixiBin"
}
finally { 
    Remove-Item -Recurse -Force $work 
}
