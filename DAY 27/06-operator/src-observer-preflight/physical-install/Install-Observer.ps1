param(
    [Parameter(Mandatory=$true)][ValidateSet('Install','Remove')][string]$Action,
    [Parameter(Mandatory=$true)][ValidatePattern('^[A-Za-z]:\\$')][string]$CardRoot,
    [Parameter(Mandatory=$true)][string]$Baseline,
    [string]$Observer
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$expected = '960f125c87258957cf07c2c13c5c83c4395f478fa0758fbffbd0263b98e49499'
$names = @('ugv_brake.lua','tank_mixer.lua.disabled','ugv_brake.lua.hold-backup','ugv_brake.pre-hold.disabled')
$dir = Join-Path $CardRoot 'APM\scripts'
$target = Join-Path $dir 'stage3_src_observer.lua'
if (-not (Test-Path -LiteralPath $dir -PathType Container)) { throw 'APM/scripts missing' }
$hashes = @{}
foreach ($line in (Get-Content -LiteralPath $Baseline)) {
    if ($line -notmatch '^([a-fA-F0-9]{64})  scripts/([^/\\]+)$') { throw 'Bad baseline line' }
    $hash, $name = $Matches[1], $Matches[2]
    if ($name -cnotin $names -or $hashes.ContainsKey($name)) { throw 'Bad baseline filename' }
    $hashes[$name] = $hash
}
if ($hashes.Count -ne 4) { throw 'Expected four baseline hashes' }

function Assert-Hash([string]$Path, [string]$Digest) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Missing file: $Path" }
    if ((Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash -ne $Digest) {
        throw "SHA256 mismatch: $Path"
    }
}
function Assert-Baseline {
    foreach ($name in $names) { Assert-Hash (Join-Path $dir $name) $hashes[$name] }
}
function Assert-NoObserver {
    if (@(Get-ChildItem -LiteralPath $dir -Force | Where-Object Name -ieq 'stage3_src_observer.lua').Count -ne 0) {
        throw 'Observer name already exists'
    }
}

Assert-Baseline
if ($Action -eq 'Install') {
    Assert-NoObserver
    if (-not $Observer) { throw 'Supply the approved Observer file' }
    Assert-Hash $Observer $expected
    $source = (Get-Item -LiteralPath $Observer).FullName
    if ((Get-Item -LiteralPath $source).Length -ne 341) { throw 'Unexpected Observer size' }
    [System.IO.File]::Copy($source, $target, $false)
    Assert-Hash $target $expected
} else {
    Assert-Hash $target $expected
    Remove-Item -LiteralPath $target -ErrorAction Stop
    Assert-NoObserver
}
Assert-Baseline
Write-Output "$Action PASS. Eject the card safely in Windows."
