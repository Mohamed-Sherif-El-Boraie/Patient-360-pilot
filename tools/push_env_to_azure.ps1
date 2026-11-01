# Copies the FILLED values of your .env file into the App Service settings. Empty values and comment lines are skipped.
# Shows only the NAMES it set, never a value. The temporary JSON file it needs is deleted at the end.
#
#   powershell -ExecutionPolicy Bypass -File tools\push_env_to_azure.ps1
#
# Changing the settings restarts the App Service, which then pulls the image again.
param(
    [string]$App = "P360-Ai-Service-Linux",
    [string]$Group = "NextGen-Patient-360",
    [string]$EnvFile = ".env"
)

$settings = @()
foreach ($line in Get-Content $EnvFile -Encoding UTF8) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.+?)\s*$') {
        $settings += [ordered]@{ name = $Matches[1]; value = $Matches[2]; slotSetting = $false }
    }
}
$names = $settings | ForEach-Object { $_.name }

if ($names -notcontains "BLOB_OUTPUT_URL_RAW") {
    Write-Host "STOP: BLOB_OUTPUT_URL_RAW is empty in $EnvFile. Without it the app refuses to start." -ForegroundColor Red
    exit 1
}
$backend = @("BACKEND_BASE_URL", "BACKEND_CALLER_SOURCE", "BACKEND_CALLER_SOURCE_KEY", "BACKEND_CALLER_USER_NAME",
             "BACKEND_USERNAME", "BACKEND_PASSWORD")
$given = @($backend | Where-Object { $names -contains $_ })
if ($given.Count -gt 0 -and $given.Count -lt $backend.Count) {
    Write-Host "STOP: only $($given.Count) of the 6 BACKEND_* settings are filled. Fill all six, or comment all of them out." -ForegroundColor Red
    exit 1
}

$file = Join-Path $env:TEMP "p360-appsettings.json"
try {
    [IO.File]::WriteAllText($file, (ConvertTo-Json -InputObject $settings), (New-Object Text.UTF8Encoding($false)))
    Write-Host "Setting $($names.Count) values on $App ..."
    az webapp config appsettings set --name $App --resource-group $Group --settings "@$file" --query "[].name" --output tsv
}
finally {
    Remove-Item $file -Force -ErrorAction SilentlyContinue
}
