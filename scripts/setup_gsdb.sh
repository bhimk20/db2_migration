#!/usr/bin/env bash
# Install IBM's GSDB (Great Outdoors Sales) sample database into a running Db2 Docker container.
#
# Everything happens inside the container: the archive is downloaded there, so nothing is copied
# from the host. Existing databases (SAMPLE, TESTDB, ...) are not touched; only a database with the
# target name is dropped and recreated (setupGSDB.sh -createdb).
#
# Usage: scripts/setup_gsdb.sh [container] [database]
#   container  Docker container running Db2           (default: db2)
#   database   Name of the database to create         (default: GS_DB)
#
# Env overrides: INSTANCE_USER (db2inst1), INSTANCE_PASSWORD (default: container's DB2INST1_PASSWORD), WORK_DIR (/database/gsdb_setup)
set -euo pipefail

CONTAINER="${1:-db2}"
DBNAME="${2:-GS_DB}"
INSTANCE_USER="${INSTANCE_USER:-db2inst1}"
WORK_DIR="${WORK_DIR:-/database/gsdb_setup}"   # on the container's persistent /database volume
# ftp.software.ibm.com no longer serves this file; public.dhe.ibm.com is the live mirror.
ARCHIVE_URL="${ARCHIVE_URL:-https://public.dhe.ibm.com/software/data/sample/GSDB_DB2_LUW_ZOS_v2r3.zip}"
ARCHIVE="$(basename "$ARCHIVE_URL")"

[[ "$DBNAME" =~ ^[A-Za-z][A-Za-z0-9_]{0,7}$ ]] || { echo "Invalid Db2 database name: $DBNAME (max 8 chars)" >&2; exit 1; }
docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true \
  || { echo "Container '$CONTAINER' is not running" >&2; exit 1; }

# Password defaults to the one the container was started with.
INSTANCE_PASSWORD="${INSTANCE_PASSWORD:-$(docker exec "$CONTAINER" printenv DB2INST1_PASSWORD || true)}"
[ -n "$INSTANCE_PASSWORD" ] || { echo "Set INSTANCE_PASSWORD (container has no DB2INST1_PASSWORD)" >&2; exit 1; }

# 1. Download + unpack inside the container (skipped when already present).
docker exec "$CONTAINER" bash -c "
  set -e
  mkdir -p '$WORK_DIR' && cd '$WORK_DIR'
  [ -s '$ARCHIVE' ] || curl -sS -f -L -m 900 -O '$ARCHIVE_URL'
  [ -d DB2/unix ]   || unzip -q -o '$ARCHIVE'
  chown -R '$INSTANCE_USER' . && chmod +x DB2/unix/*.sh DB2/unix/GS_SCRIPTS/*.sh   # setup writes SQL/logs next to itself
"

# 2. Run IBM's setup as the instance owner. -createdb creates DB, bufferpool and tablespace.
echo "Creating $DBNAME in $CONTAINER (takes a few minutes)..."
docker exec "$CONTAINER" su - "$INSTANCE_USER" -c "
  cd '$WORK_DIR/DB2/unix' &&
  ./setupGSDB.sh -createdb -database '$DBNAME' -userid '$INSTANCE_USER' -password '$INSTANCE_PASSWORD' -noprompt
" 2>&1 | tee "${TMPDIR:-/tmp}/setup_gsdb.log" | tail -n 25

# 3. Verify: list schemas and tables loaded.
docker exec "$CONTAINER" su - "$INSTANCE_USER" -c "
  db2 -x connect to '$DBNAME' >/dev/null &&
  db2 -x \"SELECT TABSCHEMA, COUNT(*) AS TABLES FROM SYSCAT.TABLES WHERE TABSCHEMA LIKE 'GOSALES%' AND TYPE='T' GROUP BY TABSCHEMA ORDER BY 1\"
"

echo "Done. Point the DB2 assessment page at database=$DBNAME (docker container=$CONTAINER)."
echo "Optional cleanup: docker exec $CONTAINER rm -rf $WORK_DIR"
