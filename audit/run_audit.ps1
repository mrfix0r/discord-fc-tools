param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+$')]
    [string]$GuildId,

    [ValidateRange(1, 3650)]
    [int]$Days = 365,

    [ValidateRange(0, 100000)]
    [int]$MaxMessagesPerChannel = 1000,

    [switch]$IncludeContent
)

$ErrorActionPreference = 'Stop'

$pythonCommand = Get-Command py -ErrorAction SilentlyContinue
if (-not $pythonCommand) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
}
if (-not $pythonCommand) {
    throw 'Python 3 not found. Install it from python.org and enable Add Python to PATH.'
}

$secureToken = Read-Host 'Paste the Discord bot token (input is hidden)' -AsSecureString
$tokenPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)

try {
    $env:DISCORD_BOT_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($tokenPointer)
    $scriptPath = Join-Path $PSScriptRoot 'discord_audit_export.py'
    $outputPath = Join-Path $PSScriptRoot 'discord-audit-report.json'

    $arguments = @(
        $scriptPath,
        '--guild-id', $GuildId,
        '--days', $Days,
        '--max-messages-per-channel', $MaxMessagesPerChannel,
        '--output', $outputPath
    )

    if ($IncludeContent) {
        $arguments += '--include-content'
    }

    & $pythonCommand.Source @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "The audit exporter exited with code $LASTEXITCODE."
    }
}
finally {
    Remove-Item Env:DISCORD_BOT_TOKEN -ErrorAction SilentlyContinue
    if ($tokenPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($tokenPointer)
    }
}

Write-Host ''
Write-Host 'Upload discord-audit-report.json to this ChatGPT conversation.' -ForegroundColor Green
