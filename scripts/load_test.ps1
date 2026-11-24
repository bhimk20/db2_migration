#Requires -Version 5.1
<#
Concurrent Db2 load inside a running container, so CPU, IOPS and connection metrics become visible
in the DB2 assessment. PowerShell port of load_test.sh.

HOW TO RUN (Windows)
  1. Start the Db2 container and make sure the SAMPLE and GS_DB databases exist
     (scripts\setup_gsdb.bat installs GS_DB).
  2. In the app, start an assessment with a sampling window at least as long as the load duration
     (sidebar > Sampling settings, "Sampling window (seconds)"), then press Re-run assessment.
  3. While it samples, run this from a Command Prompt or PowerShell in the repo folder:
         scripts\load_test.bat db2 120
     or:  powershell -ExecutionPolicy Bypass -File scripts\load_test.ps1 db2 120
  4. When the assessment finishes, open the Databases tab: GS_DB (6 workers) should show clearly
     higher CPU, IOPS and connections than SAMPLE (2 workers).

  container   wslc container running Db2                    (default: db2)
  duration_s  How long to keep the load running, in seconds (default: 120)
  profile     steady | ramp | burst                          (default: steady)
                steady  all workers flat out
                ramp    workers connect one by one, evenly spread over the duration
                burst   all connected, 30 s busy / 30 s idle (use "Seconds per sample" of 10 or less)

What it does: 2 workers on SAMPLE and 6 on GS_DB (schema GOSALES). Each worker is one container
session that connects once, then loops COUNT(*) scans of the largest tables plus inserts into a
scratch table (LOAD_TEST_SCRATCH, created and dropped by this script). Press Ctrl+C to stop early;
the workers are stopped and the scratch tables dropped either way.

Container CLI: wslc only. Env override: INSTANCE_USER (db2inst1).
#>
param(
    [string]$Container = 'db2',
    [int]$Duration = 120,
    [ValidateSet('steady', 'ramp', 'burst')][string]$Profile = 'steady'
)

$ErrorActionPreference = 'Stop'
$BurstOn = 30; $BurstOff = 30
$InstanceUser = if ($env:INSTANCE_USER) { $env:INSTANCE_USER } else { 'db2inst1' }

$Cli = 'wslc'
if (-not (Get-Command $Cli -ErrorAction SilentlyContinue)) { throw 'wslc was not found on PATH' }

# Run a bash script as the instance owner inside the container. The script goes in on stdin,
# which avoids Windows quoting problems. Returns the output lines; exit code is in $LASTEXITCODE.
function Invoke-Db2Bash {
    param([string]$Script)
    $lf = ($Script -replace "`r`n", "`n").TrimEnd() + "`n"
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $lf | & $Cli exec -i $Container su - $InstanceUser -c 'bash -s' 2>$null }
    finally { $ErrorActionPreference = $prev }
}

function Invoke-Sql {
    param([string]$Database, [string]$Statement)
    Invoke-Db2Bash "db2 connect to $Database >/dev/null && db2 -x `"$Statement`""
}

function Test-ContainerRunning {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Cli exec $Container true 2>$null | Out-Null; return ($LASTEXITCODE -eq 0) }
    finally { $ErrorActionPreference = $prev }
}

# Up to two largest user tables matching the schema condition.
function Get-TopTables {
    param([string]$Database, [string]$Condition)
    $sql = "SELECT RTRIM(TABSCHEMA) || '.' || RTRIM(TABNAME) FROM SYSCAT.TABLES WHERE TYPE = 'T' AND $Condition ORDER BY CARD DESC FETCH FIRST 2 ROWS ONLY WITH UR"
    @(Invoke-Sql $Database $sql | ForEach-Object { "$_".Trim() } | Where-Object { $_ -match '^[A-Za-z0-9_.]+$' })
}

# Container-side bash for one worker: one CONNECT, then scans and inserts until the end epoch.
# The "loadtest-worker" comment lets cleanup find the processes with pkill.
function Get-WorkerScript {
    param([string]$Database, [int]$Id, [long]$End, [long]$Begin, [long]$Start, [string[]]$Tables)
    $scans = ($Tables | ForEach-Object { "echo 'SELECT COUNT(*) FROM $_ WITH UR;'; " }) -join ''
    $gate = ''
    if ($Profile -eq 'burst') {
        $gate = "[ `$(( (`$(date +%s) - $Start) % $($BurstOn + $BurstOff) )) -ge $BurstOn ] && { sleep 1; continue; }; "
    }
    @"
# loadtest-worker $Database-$Id
while [ `$(date +%s) -lt $Begin ]; do sleep 1; done
{ echo 'CONNECT TO $Database;'; n=0
  while [ `$(date +%s) -lt $End ]; do
    ${gate}${scans}n=`$((n+1)); echo "INSERT INTO LOAD_TEST_SCRATCH VALUES (`$n, CURRENT TIMESTAMP);"
  done
  echo 'CONNECT RESET;'; } | db2 -t >/dev/null
"@
}

if ($Duration -le 0) { throw 'duration_s must be a positive whole number of seconds' }
if (-not (Test-ContainerRunning)) { throw "Container '$Container' is not running" }

$jobs = @()
try {
    $start = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $end = $start + $Duration
    Write-Host ("Start: {0} container={1} duration={2}s profile={3} ({4})" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Container, $Duration, $Profile, $Cli)

    foreach ($g in @(
        @{ Db = 'SAMPLE'; Cond = "TABSCHEMA NOT LIKE 'SYS%' AND TABSCHEMA <> 'NULLID'"; Count = 2 },
        @{ Db = 'GS_DB';  Cond = "TABSCHEMA = 'GOSALES'"; Count = 6 })) {
        Invoke-Sql $g.Db 'DROP TABLE LOAD_TEST_SCRATCH' | Out-Null
        Invoke-Sql $g.Db 'CREATE TABLE LOAD_TEST_SCRATCH (ID INT, TS TIMESTAMP)' | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Cannot use database $($g.Db) in container $Container" }
        $tables = Get-TopTables $g.Db $g.Cond
        for ($i = 1; $i -le $g.Count; $i++) {
            $begin = if ($Profile -eq 'ramp') { $start + [long][math]::Floor(($i - 1) * $Duration / $g.Count) } else { $start }
            $script = Get-WorkerScript $g.Db $i $end $begin $start $tables
            # Each job is one `exec` session (one Db2 connection) kept open for the whole duration.
            $jobs += Start-Job -ArgumentList $Cli, $Container, $InstanceUser, $script -ScriptBlock {
                param($cli, $container, $user, $script)
                $ErrorActionPreference = 'Continue'
                (($script -replace "`r`n", "`n").TrimEnd() + "`n") | & $cli exec -i $container su - $user -c 'bash -s' *>$null
            }
        }
        $what = if ($tables) { $tables -join ' ' } else { 'none (inserts only)' }
        Write-Host "  $($g.Db): $($g.Count) workers, scanning: $what"
    }
    Wait-Job -Job $jobs | Out-Null
} finally {
    # Runs on normal end and on Ctrl+C: stop workers, then drop the scratch tables.
    $jobs | Stop-Job -ErrorAction SilentlyContinue
    $jobs | Remove-Job -Force -ErrorAction SilentlyContinue
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & $Cli exec $Container pkill -f loadtest-worker 2>$null | Out-Null
    $ErrorActionPreference = $prev
    foreach ($db in 'SAMPLE', 'GS_DB') { Invoke-Sql $db 'DROP TABLE LOAD_TEST_SCRATCH' | Out-Null }
    Write-Host ("End:   {0} load stopped, scratch tables dropped" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
}
