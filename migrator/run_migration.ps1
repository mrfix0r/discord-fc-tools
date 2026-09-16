[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [ValidatePattern("^[1-9][0-9]+$")]
    [string]$GuildId,
    [switch]$Apply,
    [string]$RollbackPath = "",
    [switch]$TokenFromClipboard
)

$ErrorActionPreference = "Stop"
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Migrator = Join-Path $ScriptRoot "discord_fc_migrator.py"
$Plan = Join-Path $ScriptRoot "migration_plan.json"
if ([string]::IsNullOrWhiteSpace($RollbackPath) -and -not (Test-Path -LiteralPath $Plan)) {
    throw "Copy migration_plan.example.json to migration_plan.json and set guild.expected_id first."
}

$Python = Get-Command py -ErrorAction SilentlyContinue
$CommandArguments = @()
if ($null -ne $Python) {
    $CommandArguments += "-3"
}
else {
    $Python = Get-Command python -ErrorAction SilentlyContinue
}

if ($null -eq $Python) {
    Write-Error "Python 3 was not found. Install it from https://www.python.org/downloads/"
    exit 1
}

$PythonPath = $Python.Source

$LoadedTokenFromClipboard = $false
if ($TokenFromClipboard) {
    $ClipboardToken = (Get-Clipboard -Raw).Trim()
    if ([string]::IsNullOrWhiteSpace($ClipboardToken)) {
        Write-Error "Clipboard does not contain a Discord bot token."
        exit 3
    }

    $env:DISCORD_BOT_TOKEN = $ClipboardToken
    $LoadedTokenFromClipboard = $true
    # Windows PowerShell 5.1 converts an empty string to $null here and throws.
    # A single space safely replaces the token while remaining cross-version compatible.
    Set-Clipboard -Value " "
}

$CommandArguments += @(
    $Migrator,
    "--guild-id",
    $GuildId,
    "--plan",
    $Plan
)

if (-not [string]::IsNullOrWhiteSpace($RollbackPath)) {
    $CommandArguments += @("--rollback", $RollbackPath)
}

if ($Apply) {
    if ([string]::IsNullOrWhiteSpace($RollbackPath)) {
        $Expected = "MIGRATE FC"
        $ConfirmFlag = "MIGRATE_FC"
    }
    else {
        $Expected = "ROLLBACK FC"
        $ConfirmFlag = "ROLLBACK_FC"
    }

    $Typed = Read-Host "Type '$Expected' to continue"
    if ($Typed -cne $Expected) {
        Write-Error "Confirmation phrase did not match. No changes were made."
        exit 2
    }
    $CommandArguments += @("--apply", "--confirm", $ConfirmFlag)
}

& $PythonPath @CommandArguments
$ProcessExitCode = $LASTEXITCODE

if ($LoadedTokenFromClipboard) {
    Remove-Item Env:DISCORD_BOT_TOKEN -ErrorAction SilentlyContinue
}

exit $ProcessExitCode
