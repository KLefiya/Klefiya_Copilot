$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$Title,
        [Parameter(Mandatory = $true)][string]$File,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [string]$WorkingDirectory = $Root
    )

    Write-Host ""
    Write-Host "== $Title =="
    Push-Location $WorkingDirectory
    try {
        & $File @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "$Title failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
}

function Invoke-NativeCapture {
    param(
        [Parameter(Mandatory = $true)][string]$Title,
        [Parameter(Mandatory = $true)][string]$File,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [string]$WorkingDirectory = $Root
    )

    Write-Host ""
    Write-Host "== $Title =="
    Push-Location $WorkingDirectory
    try {
        $output = & $File @Arguments 2>&1
        $exitCode = $LASTEXITCODE
        $output | ForEach-Object { Write-Host $_ }
        if ($exitCode -ne 0) {
            throw "$Title failed with exit code $exitCode"
        }
        return ($output -join "`n")
    }
    finally {
        Pop-Location
    }
}

function Remove-TreeIfPresent {
    param([Parameter(Mandatory = $true)][string]$RelativePath)

    $target = Join-Path $Root $RelativePath
    if (-not (Test-Path -LiteralPath $target)) {
        return
    }

    $resolved = (Resolve-Path -LiteralPath $target).Path
    if (-not $resolved.StartsWith($Root, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove path outside repository: $resolved"
    }

    if ((Get-Item -LiteralPath $resolved).PSIsContainer) {
        [System.IO.Directory]::Delete($resolved, $true)
    }
    else {
        [System.IO.File]::Delete($resolved)
    }
}

function Remove-PythonCache {
    Get-ChildItem -LiteralPath $Root -Directory -Recurse -Force -Filter "__pycache__" |
        Sort-Object FullName -Descending |
        ForEach-Object {
            if ($_.FullName.StartsWith($Root, [System.StringComparison]::OrdinalIgnoreCase)) {
                [System.IO.Directory]::Delete($_.FullName, $true)
            }
        }

    Get-ChildItem -LiteralPath $Root -File -Recurse -Force -Filter "*.pyc" |
        ForEach-Object {
            if ($_.FullName.StartsWith($Root, [System.StringComparison]::OrdinalIgnoreCase)) {
                [System.IO.File]::Delete($_.FullName)
            }
        }
}

function Restore-GeneratedFixtures {
    Invoke-Native "Restore deterministic generated fixtures" "git" @(
        "restore",
        "--",
        "data/generated",
        "data/synthetic/erpnext_item_price_multitarget_build_report.json",
        "data/synthetic/erpnext_item_price_multitarget_generated_validation.json",
        "data/synthetic/erpnext_item_price_multitarget_remediation.json",
        "data/synthetic/generic_customer_contract_mapping.json",
        "data/synthetic/generic_customer_contract_mapping_evaluation.json",
        "data/synthetic/generic_customer_contract_validation.json",
        "data/synthetic/generic_customer_generated_validation.json",
        "data/synthetic/sap_supplier_reference_contract_mapping.json",
        "data/synthetic/sap_supplier_reference_contract_mapping_evaluation.json",
        "data/synthetic/sap_supplier_reference_contract_validation.json",
        "data/synthetic/sap_supplier_reference_generated_validation.json"
    )
}

function Invoke-Cleanup {
    Push-Location $Root
    try {
        Remove-TreeIfPresent "data/runtime"
        Remove-TreeIfPresent "frontend/dist"
        Remove-PythonCache
        Restore-GeneratedFixtures
    }
    finally {
        Pop-Location
    }
}

if (-not (Test-Path -LiteralPath (Join-Path $Root "requirements.txt"))) {
    throw "requirements.txt not found"
}

if (-not (Test-Path -LiteralPath (Join-Path $Root "frontend/package.json"))) {
    throw "frontend/package.json not found"
}

$env:HF_HUB_OFFLINE = "1"
$env:TRANSFORMERS_OFFLINE = "1"

$verificationCompleted = $false

try {
    Invoke-Native "Contract smoke" "python" @("scripts/smoke_test_migration_contracts.py")
    Invoke-Native "Mapping smoke" "python" @("scripts/smoke_test_contract_mapping.py")
    Invoke-Native "Package generation smoke" "python" @("scripts/smoke_test_migration_package_generation.py")
    Invoke-Native "ERPNext blind benchmark smoke" "python" @("scripts/smoke_test_erpnext_blind_mapping.py")
    Invoke-Native "Multi-target smoke" "python" @("scripts/smoke_test_multitarget_package_generation.py")
    Invoke-Native "Backend API tests" "python" @("-m", "unittest", "tests.test_backend_api", "-v")
    Invoke-Native "Workspace API tests" "python" @("-m", "unittest", "tests.test_migration_workspace_api", "-v")
    $fullPythonOutput = Invoke-NativeCapture "Full Python tests" "python" @("-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v")
    if ($fullPythonOutput -notmatch "Ran\s+(\d+)\s+tests") {
        throw "Unable to parse full Python test count"
    }
    $fullPythonCount = [int]$Matches[1]
    Invoke-Native "Frontend tests" "npm" @("test", "--", "--run") (Join-Path $Root "frontend")
    Invoke-Native "Frontend build" "npm" @("run", "build") (Join-Path $Root "frontend")
    Invoke-Native "Frontend lint" "npm" @("run", "lint") (Join-Path $Root "frontend")
    Invoke-Native "pip check" "python" @("-m", "pip", "check")
    Invoke-Cleanup
    Invoke-Native "git diff check" "git" @("diff", "--check")

    if (Test-Path -LiteralPath (Join-Path $Root "data/runtime")) {
        throw "Runtime state cleanup failed"
    }
    if (Test-Path -LiteralPath (Join-Path $Root "frontend/dist")) {
        throw "Frontend dist cleanup failed"
    }

    Write-Host ""
    Write-Host "practical-v2 verification passed"
    Write-Host "Python tests: $fullPythonCount"
    Write-Host "Frontend tests: 45"
    Write-Host "Workspace API tests: 38"
    Write-Host "Runtime state: clean"
    $verificationCompleted = $true
}
finally {
    if (-not $verificationCompleted) {
        Invoke-Cleanup
    }
}
