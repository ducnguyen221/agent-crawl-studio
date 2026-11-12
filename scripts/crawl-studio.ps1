# Launcher for agent-crawl-studio: runs `python -m agent_crawl_studio` from the core venv owned by
# this repo (runtime/venvs/agent-crawl-studio/core). Code runs from this checkout via PYTHONPATH;
# nothing is installed into the venv from the repo (opcos-runtime.json declares no install.package).
# Usage: scripts\crawl-studio.ps1 <subcommand> [args...]
# stdout is ONE JSON line. If the runtime tool or the venv is missing, this launcher prints a
# DEPENDENCY_MISSING JSON line itself (it never installs anything, never looks for another Python): exit 0 for
# `capabilities` (SPR-01 contract), exit 2 for `plan` and `run` (the request is refused before running, SPEC FR1.7).
# Otherwise the exit code is the one of the Python process; decide success by $LASTEXITCODE.
$ErrorActionPreference = 'Stop'
$missingExit = 0
if ($args.Count -gt 0 -and ($args[0] -eq 'plan' -or $args[0] -eq 'run')) { $missingExit = 2 }

function Write-Missing {
    $line = '{"schema_version":1,"venv":{"status":"missing"},"error":{"code":"DEPENDENCY_MISSING",' +
        '"message":"A required dependency is missing; nothing was installed.",' +
        '"hint":"run: opcos-runtime ensure --repo-dir plugins/agent-crawl-studio"}}'
    [Console]::Out.WriteLine($line)
    exit $script:missingExit
}

$repo = Split-Path -Parent $PSScriptRoot
$opcos = $env:OPCOS_ROOT
if (-not $opcos) { $opcos = Join-Path $HOME 'OpcOS' }
$rt = Join-Path $opcos 'system\operations\runtime\opcos-runtime.ps1'
if (-not (Test-Path -LiteralPath $rt)) { Write-Missing }

$py = $null
try {
    $py = & $rt resolve venvs agent-crawl-studio core --python 2>$null | Select-Object -Last 1
    if ($LASTEXITCODE -ne 0) { $py = $null }
} catch {
    $py = $null
}
if (-not $py -or -not (Test-Path -LiteralPath $py)) { Write-Missing }

$env:PYTHONPATH = Join-Path $repo 'src'
$env:PYTHONDONTWRITEBYTECODE = '1'
& $py -X utf8 -m agent_crawl_studio @args
exit $LASTEXITCODE
