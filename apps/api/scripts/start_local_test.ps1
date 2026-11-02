param([switch]$ApiOnly, [switch]$ConfigurationOnly)
$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../..'))
$profile = Join-Path $root 'local-data/profiles/fresh-user-test'
$glossary = Join-Path $profile 'glossary.sqlite3'
$memory = Join-Path $profile 'translation-memory.sqlite3'
$python = Join-Path $root '.venv/Scripts/python.exe'
$configuration = & $python (Join-Path $PSScriptRoot 'local_test_data.py')
if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve the pinned fresh-user-test configuration.' }
$data = ($configuration -join "`n") | ConvertFrom-Json
if ($ConfigurationOnly) { $configuration; return }
$ocr = $data.ocr_database_path
$model = Join-Path $root 'local-models/hplt-en-sl-v2-ct2-int8'
foreach ($path in @($glossary, $memory, $python, (Join-Path $model 'model.bin'))) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Required existing local file missing: $path. No download or empty database will be created." }
}
$ports = if ($ApiOnly) { @(8000) } else { @(8000, 3001) }
foreach ($port in $ports) {
    if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
        throw "Port $port is already in use. Existing sessions were not stopped."
    }
}
if (-not $ApiOnly) {
    if (-not (Test-Path -LiteralPath (Join-Path $root 'apps/web/.next/BUILD_ID'))) { throw 'Run pnpm run build in apps/web first.' }
    $node = (Get-Command node -ErrorAction Stop).Source
}
$env:LOCAL_DATA_PROFILE = 'fresh-user-test'
$env:GLOSSARY_DATABASE_PATH = $glossary
$env:TRANSLATION_MEMORY_DATABASE_PATH = $memory
$env:OCR_DATABASE_PATH = $ocr
$env:LOCAL_TRANSLATION_PROFILE = 'hplt-en-sl-v2'
$env:LOCAL_TRANSLATION_MODEL_PATH = $model
$env:LOCAL_TRANSLATION_DEVICE = 'cpu'
$env:LOCAL_TRANSLATION_COMPUTE_TYPE = 'int8'
$api = Start-Process -FilePath $python -ArgumentList '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000' -WorkingDirectory (Join-Path $root 'apps/api') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $profile 'api.stdout.log') -RedirectStandardError (Join-Path $profile 'api.stderr.log')
Write-Output "API launcher PID $($api.Id): fresh-user-test; glossary=$glossary; TM=$memory; OCR=$ocr (the venv may launch a child server process)"
if (-not $ApiOnly) {
    $web = Start-Process -FilePath $node -ArgumentList 'node_modules/next/dist/bin/next', 'start', '--hostname', '127.0.0.1', '--port', '3001' -WorkingDirectory (Join-Path $root 'apps/web') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $profile 'frontend.stdout.log') -RedirectStandardError (Join-Path $profile 'frontend.stderr.log')
    Write-Output "Frontend PID $($web.Id): http://127.0.0.1:3001 (existing production build)"
}
