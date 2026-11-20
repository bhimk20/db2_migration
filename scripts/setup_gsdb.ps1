#Requires -Version 5.1
<#
Install IBM's GSDB (Great Outdoors Sales) sample database into a running Db2 Docker container.
PowerShell port of setup_gsdb.sh.

Everything happens inside the container: the archive is downloaded there, so nothing is copied
from the host. Existing databases (SAMPLE, TESTDB, ...) are not touched; only a database with the
target name is dropped and recreated (setupGSDB.sh -createdb).

Usage: scripts\setup_gsdb.bat [container] [database]   (or: powershell -File scripts\setup_gsdb.ps1 ...)
  container  Docker container running Db2           (default: db2)
  database   Name of the database to create         (default: GS_DB)

Env overrides: INSTANCE_USER (db2inst1), INSTANCE_PASSWORD (default: container's DB2INST1_PASSWORD),
WORK_DIR (/database/gsdb_setup), ARCHIVE_URL (default: IBM GSDB archive).
#>
param(
    [string]$Container = 'db2',
    [string]$Database = 'GS_DB'
)

$ErrorActionPreference = 'Stop'
$OutputEncoding = New-Object System.Text.UTF8Encoding $false
$LogFile = Join-Path $env:TEMP 'setup_gsdb.log'

$InstanceUser = if ($env:INSTANCE_USER) { $env:INSTANCE_USER } else { 'db2inst1' }
$WorkDir = if ($env:WORK_DIR) { $env:WORK_DIR } else { '/database/gsdb_setup' }   # on the container's persistent /database volume
# ftp.software.ibm.com no longer serves this file; public.dhe.ibm.com is the live mirror.
$ArchiveUrl = if ($env:ARCHIVE_URL) { $env:ARCHIVE_URL } else { 'https://public.dhe.ibm.com/software/data/sample/GSDB_DB2_LUW_ZOS_v2r3.zip' }
$Archive = $ArchiveUrl.Substring($ArchiveUrl.LastIndexOf('/') + 1)

# Run a docker command with its stderr discarded; returns the output and the exit code.
function Get-DockerOutput {
    param([string[]]$Arguments)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & docker @Arguments 2>$null
    } finally {
        $ErrorActionPreference = $prev
    }
    return [pscustomobject]@{ Output = $out; Code = $LASTEXITCODE }
}

# Pipe a bash script into the container and append everything it prints to the log.
function Invoke-ContainerBash {
    param([string]$Script)
    # The here-strings carry CRLF from this file; bash must only see LF.
    $lf = $Script -replace "`r`n", "`n"
    # PowerShell terminates the piped text with CRLF; a trailing comment keeps that stray CR harmless.
    $lf = $lf.TrimEnd() + "`n# end"
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'   # native stderr lines must not abort the script
    try {
        $lines = $lf | docker exec -i $Container bash -s 2>&1 | Tee-Object -FilePath $LogFile -Append
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    if ($code -ne 0) { throw "Step failed inside container '$Container' (exit $code). See $LogFile" }
    return $lines
}

# Replace __PLACEHOLDERS__ in a bash template with values.
function Expand-Template {
    param([string]$Template, [hashtable]$Values)
    foreach ($key in $Values.Keys) { $Template = $Template.Replace($key, [string]$Values[$key]) }
    return $Template
}

if ($Database -notmatch '^[A-Za-z][A-Za-z0-9_]{0,7}$') {
    throw "Invalid Db2 database name: $Database (max 8 chars)"
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'docker was not found on PATH' }

$inspect = Get-DockerOutput @('inspect', '-f', '{{.State.Running}}', $Container)
if ($inspect.Code -ne 0 -or ("$($inspect.Output)").Trim() -ne 'true') {
    throw "Container '$Container' is not running"
}

# Password defaults to the one the container was started with.
if ($env:INSTANCE_PASSWORD) {
    $InstancePassword = $env:INSTANCE_PASSWORD
} else {
    $pw = Get-DockerOutput @('exec', $Container, 'printenv', 'DB2INST1_PASSWORD')
    $InstancePassword = ("$($pw.Output)").Trim()
}
if (-not $InstancePassword) { throw 'Set INSTANCE_PASSWORD (container has no DB2INST1_PASSWORD)' }

$values = @{
    '__WORK_DIR__'         = $WorkDir
    '__ARCHIVE_URL__'      = $ArchiveUrl
    '__ARCHIVE__'          = $Archive
    '__INSTANCE_USER__'    = $InstanceUser
    '__INSTANCE_PASSWORD__' = $InstancePassword
    '__DATABASE__'         = $Database
}

Set-Content -Path $LogFile -Encoding Unicode -Value "setup_gsdb $(Get-Date -Format s) container=$Container database=$Database"

# 1. Download + unpack inside the container (skipped when already present).
$template1 = @'
set -e
mkdir -p '__WORK_DIR__' && cd '__WORK_DIR__'
[ -s '__ARCHIVE__' ] || curl -sS -f -L -m 900 -O '__ARCHIVE_URL__' </dev/null
[ -d DB2/unix ]   || unzip -q -o '__ARCHIVE__' </dev/null
chown -R '__INSTANCE_USER__' . && chmod +x DB2/unix/*.sh DB2/unix/GS_SCRIPTS/*.sh   # setup writes SQL/logs next to itself
'@
[void](Invoke-ContainerBash (Expand-Template $template1 $values))

# 2. Run IBM's setup as the instance owner. -createdb creates DB, bufferpool and tablespace.
Write-Host "Creating $Database in $Container (takes a few minutes)..."
$template2 = @'
su - '__INSTANCE_USER__' -c "cd '__WORK_DIR__/DB2/unix' && ./setupGSDB.sh -createdb -database '__DATABASE__' -userid '__INSTANCE_USER__' -password '__INSTANCE_PASSWORD__' -noprompt" </dev/null
'@
$setupOutput = Invoke-ContainerBash (Expand-Template $template2 $values)
$setupOutput | Select-Object -Last 25 | ForEach-Object { Write-Host $_ }

# 3. Verify: list schemas and tables loaded.
$template3 = @'
su - '__INSTANCE_USER__' -c "db2 -x connect to '__DATABASE__' >/dev/null && db2 -x \"SELECT TABSCHEMA, COUNT(*) AS TABLES FROM SYSCAT.TABLES WHERE TABSCHEMA LIKE 'GOSALES%' AND TYPE='T' GROUP BY TABSCHEMA ORDER BY 1\"" </dev/null
'@
$verifyOutput = Invoke-ContainerBash (Expand-Template $template3 $values)
$verifyOutput | ForEach-Object { Write-Host $_ }

Write-Host "Done. Point the DB2 assessment page at database=$Database (docker container=$Container)."
Write-Host "Optional cleanup: docker exec $Container rm -rf $WorkDir"
