# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
<#
.SYNOPSIS
    Creates the development venv on Windows and installs AFMLabel with the dev extra.

.EXAMPLE
    .\scripts\dev\bootstrap.ps1
    .\scripts\dev\bootstrap.ps1 -PythonVersion 3.12 -WithBrowsers
#>
[CmdletBinding()]
param(
    [string]$PythonVersion = "3.13",
    [switch]$WithBrowsers
)

$ErrorActionPreference = 'Stop'
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot '..\..')
Push-Location $repoRoot
try {
    if (-not (Test-Path '.venv')) {
        & py "-$PythonVersion" -m venv .venv
    }
    $python = Join-Path $repoRoot '.venv\Scripts\python.exe'
    & $python -m pip install --upgrade pip
    & $python -m pip install -e ".[dev]"
    if ($WithBrowsers) {
        & $python -m playwright install chromium firefox
    }
    New-Item -ItemType Directory -Force -Path 'tmp' | Out-Null
    Write-Host "Done. Activate with: .\.venv\Scripts\Activate.ps1" -ForegroundColor Cyan
}
finally {
    Pop-Location
}
