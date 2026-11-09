# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
<#
.SYNOPSIS
    Runs lint, type checks and the CPU-only test suite (optionally browser tests).

.EXAMPLE
    .\scripts\dev\check.ps1
    .\scripts\dev\check.ps1 -Browser
#>
[CmdletBinding()]
param(
    [switch]$Browser
)

$ErrorActionPreference = 'Stop'
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
Push-Location $repoRoot
try {
    & $python -m ruff check src tests scripts
    if (-not $?) { throw "ruff failed" }
    & $python -m mypy src
    if (-not $?) { throw "mypy failed" }
    & $python -m pytest -m "not browser and not real_model and not gpu"
    if (-not $?) { throw "pytest failed" }
    & $python scripts/add_license_headers.py --check
    if (-not $?) { throw "licence header check failed" }
    & $python scripts/generate_help_reference.py --check
    if (-not $?) { throw "user-guide shortcut table is stale" }
    if ($Browser) {
        & $python -m pytest -m browser --browser chromium
        if (-not $?) { throw "browser tests failed" }
    }
    & $python -m afmlabel.diagnostics.privacy
    if (-not $?) { throw "privacy diagnostic failed" }
    Write-Host "All checks passed." -ForegroundColor Green
}
finally {
    Pop-Location
}
