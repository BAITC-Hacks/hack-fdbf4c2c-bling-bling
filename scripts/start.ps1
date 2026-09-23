param(
    [string]$N8nContainer = 'hackathon_n8n',
    [switch]$ManagedN8n,
    [switch]$PrepareModels,
    [switch]$SkipBuild,
    [switch]$SkipImport
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $projectRoot
$dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
$dockerExe = if ($dockerCommand) { $dockerCommand.Source } else { Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\resources\bin\docker.exe' }
if (-not (Test-Path -LiteralPath $dockerExe)) { throw 'Docker Desktop is required.' }
function Invoke-Docker {
    & $dockerExe @args
    if ($LASTEXITCODE -ne 0) { throw "Docker failed: $($args[0])" }
}
New-Item -ItemType Directory -Path .runtime -Force | Out-Null
if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
$envLines = Get-Content -LiteralPath '.env'
$values = @{}
foreach ($line in $envLines) { if ($line -match '^([A-Z_]+)=(.*)$') { $values[$matches[1]] = $matches[2] } }
$secretKeys = @('POSTGRES_PASSWORD','SERVICE_TOKEN','WEBHOOK_TOKEN','APP_SECRET','ADMIN_PASSWORD','N8N_ENCRYPTION_KEY','QDRANT_API_KEY')
foreach ($key in $secretKeys) {
    if (-not $values[$key] -or $values[$key] -eq 'generate-with-scripts-init_env.py') {
        $bytes = New-Object byte[] 32
        [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
        $values[$key] = [Convert]::ToHexString($bytes).ToLowerInvariant()
    }
}
foreach ($entry in @{'ADMIN_EMAIL'='admin@local.test';'LLM_MODEL'='qwen3:4b';'EMBEDDING_MODEL'='qwen3-embedding:0.6b';'ASR_MODEL'='/models/whisper-small';'SPEAKER_MODEL'='/models/speaker.onnx';'N8N_URL'='http://n8n:5678'}.GetEnumerator()) { if (-not $values[$entry.Key]) { $values[$entry.Key] = $entry.Value } }
# Update only project configuration keys, retaining comments and other values.
$written = @{}
$updated = foreach ($line in $envLines) {
    if ($line -match '^([A-Z_]+)=(.*)$' -and $values.ContainsKey($matches[1])) { $key=$matches[1]; $written[$key]=$true; "$key=$($values[$key])" } else { $line }
}
foreach ($key in $values.Keys) { if (-not $written.ContainsKey($key)) { $updated += "$key=$($values[$key])" } }
[IO.File]::WriteAllLines((Join-Path $projectRoot '.env'), [string[]]$updated)
$compose = @('compose','--env-file','.env','-f','deploy/compose.yaml')
if ($ManagedN8n) { $compose += @('--profile','managed-n8n'); $N8nContainer = 'hackalem-n8n-1' }
Invoke-Docker @compose config --quiet
if (-not $SkipBuild) { Invoke-Docker @compose build api }
if ($PrepareModels) {
    # Only this project's services/network are recreated. Named volumes survive.
    if (-not $ManagedN8n) {
        $networkState = & $dockerExe inspect $N8nContainer --format '{{json .NetworkSettings.Networks}}'
        if ($networkState -match 'hackalem_private') { Invoke-Docker network disconnect hackalem_private $N8nContainer }
    }
    Invoke-Docker @compose down
    $prepare = $compose + @('-f','deploy/compose.prepare.yaml')
    Invoke-Docker @prepare up -d postgres ollama qdrant
    Invoke-Docker @prepare exec -T ollama ollama pull $values['LLM_MODEL']
    Invoke-Docker @prepare exec -T ollama ollama pull $values['EMBEDDING_MODEL']
    Invoke-Docker @prepare run --rm --no-deps api python scripts/prepare_models.py
    Invoke-Docker @prepare down
}
Invoke-Docker @compose up -d postgres ollama qdrant api worker web
if ($ManagedN8n) { Invoke-Docker @compose up -d n8n n8n-web }
$networkState = & $dockerExe inspect $N8nContainer --format '{{json .NetworkSettings.Networks}}'
if ($LASTEXITCODE -ne 0) { throw 'Existing n8n container was not found. See README for managed-n8n profile.' }
if ($networkState -notmatch 'hackalem_private') { Invoke-Docker network connect --alias n8n hackalem_private $N8nContainer }
if (-not $SkipImport) {
    $credentials = @(
        @{id='haBackendCred001';name='HackAlem backend';type='httpHeaderAuth';data=@{name='Authorization';value="Bearer $($values['SERVICE_TOKEN'])"}},
        @{id='haEventCred00001';name='HackAlem event ingress';type='httpHeaderAuth';data=@{name='X-Hackalem-Event';value=$values['WEBHOOK_TOKEN']}},
        @{id='haOllamaCred0001';name='HackAlem Ollama local';type='ollamaApi';data=@{baseUrl='http://ollama:11434'}}
    )
    $credentialFile = Join-Path $projectRoot '.runtime/n8n-credentials.json'
    [IO.File]::WriteAllText($credentialFile, ($credentials | ConvertTo-Json -Depth 6))
    Invoke-Docker exec $N8nContainer n8n export:workflow --all --output=/tmp/hackalem-before.json
    Invoke-Docker cp "${N8nContainer}:/tmp/hackalem-before.json" .runtime/n8n-before-import.json
    Invoke-Docker cp $credentialFile "${N8nContainer}:/tmp/hackalem-credentials.json"
    Invoke-Docker cp workflows/all.json "${N8nContainer}:/tmp/hackalem-workflows.json"
    try {
        Invoke-Docker exec $N8nContainer n8n import:credentials --input=/tmp/hackalem-credentials.json
        Invoke-Docker exec $N8nContainer n8n import:workflow --input=/tmp/hackalem-workflows.json
        $workflows = Get-Content workflows/all.json -Raw | ConvertFrom-Json
        foreach ($workflow in $workflows) { Invoke-Docker exec $N8nContainer n8n publish:workflow "--id=$($workflow.id)" }
    } finally {
        Invoke-Docker exec -u 0 $N8nContainer rm -f /tmp/hackalem-credentials.json
        Remove-Item -LiteralPath $credentialFile -ErrorAction SilentlyContinue
    }
    # CLI publication requires reloading the running n8n process.
    Invoke-Docker restart $N8nContainer
}
Write-Host 'n8n: http://localhost:5678 | Application: http://localhost:8080'
Write-Host 'Application credentials: ADMIN_EMAIL / ADMIN_PASSWORD in local .env.'
