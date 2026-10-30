param(
    [Parameter(Mandatory=$true, ValueFromRemainingArguments=$true)]
    [string[]]$cmd
)
$command = $cmd -join " "

# Load settings from .env.prod or .env if present
$envFile = Join-Path $PSScriptRoot ".env.prod"
if (-not (Test-Path $envFile)) { $envFile = Join-Path $PSScriptRoot ".env" }
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match "^\s*DEPLOY_SERVER_HOST\s*=\s*(.+)$") { $env:DEPLOY_SERVER_HOST = $matches[1].Trim() }
        if ($_ -match "^\s*DEPLOY_SSH_KEY\s*=\s*(.+)$") { $env:DEPLOY_SSH_KEY = $matches[1].Trim() }
    }
}

$serverHost = if ($env:DEPLOY_SERVER_HOST) { $env:DEPLOY_SERVER_HOST } else { Read-Host "Enter server host/IP" }
$keyPath = if ($env:DEPLOY_SSH_KEY) { $env:DEPLOY_SSH_KEY } else { "$env:USERPROFILE\.ssh\id_rsa" }

ssh.exe -i $keyPath -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL "root@$serverHost" $command
