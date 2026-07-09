param(
    [Parameter(Mandatory = $true)]
    [string]$RepoUrl,

    [Parameter(Mandatory = $true)]
    [string]$RunnerToken,

    [string]$RunnerRoot = 'C:\actions-runner\stockbot-trader',
    [string]$RunnerName = $env:COMPUTERNAME,
    [string]$Labels = 'self-hosted,windows,stockbot-trader',
    [string]$WorkFolder = '_work'
)

$ErrorActionPreference = 'Stop'

function Ensure-Admin {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Please run this script in an elevated PowerShell session.'
    }
}

function Get-LatestRunnerAsset {
    $headers = @{ 'User-Agent' = 'stockbot-self-hosted-runner-bootstrap' }
    $release = Invoke-RestMethod -Uri 'https://api.github.com/repos/actions/runner/releases/latest' -Headers $headers
    $asset = $release.assets | Where-Object { $_.name -match '^actions-runner-win-x64-.*\.zip$' } | Select-Object -First 1
    if (-not $asset) {
        throw 'Unable to resolve the latest Windows x64 runner asset.'
    }
    return $asset
}

Ensure-Admin

New-Item -ItemType Directory -Force -Path $RunnerRoot | Out-Null
$asset = Get-LatestRunnerAsset
$zipPath = Join-Path $RunnerRoot $asset.name

Write-Host "Downloading runner package: $($asset.browser_download_url)"
Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zipPath
Expand-Archive -Path $zipPath -DestinationPath $RunnerRoot -Force
Remove-Item $zipPath -Force

Push-Location $RunnerRoot
try {
    $configCmd = Join-Path $RunnerRoot 'config.cmd'
    if (-not (Test-Path $configCmd)) {
        throw "config.cmd not found in $RunnerRoot"
    }
    $configArgs = @(
        '--url', $RepoUrl,
        '--token', $RunnerToken,
        '--name', $RunnerName,
        '--labels', $Labels,
        '--work', $WorkFolder,
        '--unattended',
        '--replace',
        '--runasservice'
    )
    Write-Host "Configuring self-hosted runner at $RunnerRoot"
    & $configCmd @configArgs
}
finally {
    Pop-Location
}

Write-Host 'Runner configured. Verify it is online in GitHub -> Settings -> Actions -> Runners.'
