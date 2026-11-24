#!/usr/bin/env bash
# Concurrent Db2 load inside a running container, to make CPU, IOPS and connection metrics visible.
# Usage: scripts/load_test.sh [container=db2] [duration_s=120] [profile=steady]
# Profiles: steady (all workers flat out), ramp (workers connect one by one, evenly spread over the
# whole duration, so load and connections climb), burst (all connected, 30 s busy / 30 s idle).
# With burst or ramp use "Seconds per sample" of 10 or less in the app.
# 2 workers on SAMPLE, 6 on GS_DB (schema GOSALES). Each worker is one exec session that connects
# once and loops COUNT(*) scans of the largest tables plus inserts into LOAD_TEST_SCRATCH.
# Env overrides: DB2_CONTAINER_CLI (docker or wslc), INSTANCE_USER (db2inst1).
set -u
CONTAINER="${1:-db2}"
DURATION="${2:-120}"
PROFILE="${3:-steady}"
INSTANCE_USER="${INSTANCE_USER:-db2inst1}"
BURST_ON=30; BURST_OFF=30
case "$PROFILE" in steady|ramp|burst) ;; *) echo "profile must be steady, ramp or burst" >&2; exit 1;; esac
[[ "$DURATION" =~ ^[0-9]+$ ]] || { echo "duration_s must be whole seconds" >&2; exit 1; }
# Container CLI: DB2_CONTAINER_CLI, else the first of docker / wslc that answers `ps`.
CLI="${DB2_CONTAINER_CLI:-}"
for c in docker wslc; do
  [ -z "$CLI" ] && command -v "$c" >/dev/null 2>&1 && "$c" ps >/dev/null 2>&1 && CLI=$c
done
[ -n "$CLI" ] || { echo "Neither docker nor wslc is available" >&2; exit 1; }
"$CLI" exec "$CONTAINER" true 2>/dev/null || { echo "Container '$CONTAINER' is not running" >&2; exit 1; }
# One SQL statement as the instance owner (db2 -x output).
sql() {  # $1 database, $2 statement
  "$CLI" exec "$CONTAINER" su - "$INSTANCE_USER" -c "db2 connect to $1 >/dev/null && db2 -x \"$2\""
}
# Up to two largest user tables matching the schema condition $2.
top_tables() {  # $1 database, $2 condition on TABSCHEMA
  sql "$1" "SELECT RTRIM(TABSCHEMA) || '.' || RTRIM(TABNAME) FROM SYSCAT.TABLES WHERE TYPE = 'T' AND $2 ORDER BY CARD DESC FETCH FIRST 2 ROWS ONLY WITH UR" |
    sed 's/[[:space:]]*$//' | grep -E '^[A-Za-z0-9_.]+$'
}
setup_db() {  # $1 database: (re)create the scratch table
  sql "$1" "DROP TABLE LOAD_TEST_SCRATCH" >/dev/null 2>&1
  sql "$1" "CREATE TABLE LOAD_TEST_SCRATCH (ID INT, TS TIMESTAMP)" >/dev/null ||
    { echo "Cannot use database $1 in container $CONTAINER" >&2; exit 1; }
}
# Container-side shell for one worker: one CONNECT, then scans and inserts until the end epoch.
# The "loadtest-worker" comment lets cleanup find the processes with pkill.
worker_cmd() {  # $1 database, $2 worker id, $3 end epoch, $4 begin epoch, $5... tables
  local db=$1 id=$2 end=$3 begin=$4 scans="" t gate=""
  shift 4
  for t in "$@"; do scans+="echo 'SELECT COUNT(*) FROM $t WITH UR;'; "; done
  if [ "$PROFILE" = burst ]; then
    gate="[ \$(( (\$(date +%s) - $START) % $((BURST_ON + BURST_OFF)) )) -ge $BURST_ON ] && { sleep 1; continue; }; "
  fi
  cat <<EOT
# loadtest-worker $db-$id
while [ \$(date +%s) -lt $begin ]; do sleep 1; done
{ echo 'CONNECT TO $db;'; n=0
  while [ \$(date +%s) -lt $end ]; do
    ${gate}${scans}n=\$((n+1)); echo "INSERT INTO LOAD_TEST_SCRATCH VALUES (\$n, CURRENT TIMESTAMP);"
  done
  echo 'CONNECT RESET;'; } | db2 -t >/dev/null
EOT
}
PIDS=()  # host-side docker exec clients
cleanup() {
  trap - EXIT
  [ "${#PIDS[@]}" -gt 0 ] && kill "${PIDS[@]}" 2>/dev/null
  "$CLI" exec "$CONTAINER" pkill -f loadtest-worker >/dev/null 2>&1
  wait 2>/dev/null
  for db in SAMPLE GS_DB; do sql "$db" "DROP TABLE LOAD_TEST_SCRATCH" >/dev/null 2>&1; done
  echo "End:   $(date '+%F %T') load stopped, scratch tables dropped"
}
trap cleanup EXIT
trap 'exit 130' INT TERM
start_group() {  # $1 database, $2 schema condition, $3 worker count
  local db=$1 cond=$2 count=$3 i begin
  local tables=()
  setup_db "$db"
  read -r -a tables <<< "$(top_tables "$db" "$cond")"
  for ((i = 1; i <= count; i++)); do
    begin=$START
    [ "$PROFILE" = ramp ] && begin=$((START + (i - 1) * DURATION / count))
    "$CLI" exec "$CONTAINER" su - "$INSTANCE_USER" -c \
      "$(worker_cmd "$db" "$i" "$END" "$begin" ${tables[@]+"${tables[@]}"})" >/dev/null 2>&1 &
    PIDS+=("$!")
  done
  echo "  $db: $count workers, scanning: ${tables[*]:-none (inserts only)}"
}
START=$(date +%s)
END=$(( START + DURATION ))
echo "Start: $(date '+%F %T') container=$CONTAINER duration=${DURATION}s profile=$PROFILE ($CLI)"
start_group SAMPLE "TABSCHEMA NOT LIKE 'SYS%' AND TABSCHEMA <> 'NULLID'" 2
start_group GS_DB "TABSCHEMA = 'GOSALES'" 6
wait
