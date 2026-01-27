$ErrorActionPreference = 'Stop'

# Load azd environment variables into current PowerShell session
$envFile = "$(Split-Path -Parent $PSScriptRoot)\.azure\$(azd env get-value AZURE_ENV_NAME)\.env"
if (-not (Test-Path $envFile)) {
    Write-Error "azd environment file not found: $envFile"
}

Get-Content $envFile | ForEach-Object {
    if ($_ -match '^(\w+)="(.*)"$') {
        [System.Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process')
    }
}

# Fallbacks used by the app
if (-not $env:AZURE_CONTAINER_APPS_SESSION_POOL_ENDPOINT -and $env:DYNAMIC_SESSION_POOL_ENDPOINT) {
    $env:AZURE_CONTAINER_APPS_SESSION_POOL_ENDPOINT = $env:DYNAMIC_SESSION_POOL_ENDPOINT
}
if (-not $env:AZURE_OPENAI_CHAT_DEPLOYMENT_NAME -and $env:AZURE_OPENAI_DEPLOYMENT) {
    $env:AZURE_OPENAI_CHAT_DEPLOYMENT_NAME = $env:AZURE_OPENAI_DEPLOYMENT
}
if (-not $env:SESSION_POOL_AUDIENCE) {
    $env:SESSION_POOL_AUDIENCE = 'https://dynamicsessions.io/.default'
}

Write-Host "Loaded azd environment variables into current session."
Write-Host "AZURE_OPENAI_ENDPOINT=$env:AZURE_OPENAI_ENDPOINT"
Write-Host "AZURE_OPENAI_CHAT_DEPLOYMENT_NAME=$env:AZURE_OPENAI_CHAT_DEPLOYMENT_NAME"
Write-Host "AZURE_CONTAINER_APPS_SESSION_POOL_ENDPOINT=$env:AZURE_CONTAINER_APPS_SESSION_POOL_ENDPOINT"
