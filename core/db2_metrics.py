"""
IBM Db2 metrics collector and analyzer.

Collects sizing inputs from a Db2 database (read-only queries) and turns them into the keyword
arguments of `azure_recommender.recommend_azure_postgresql`, plus a list of migration findings.

Two ways to reach Db2:
  - docker mode: `docker exec <container> su - <instance> -c "db2 ..."` (no driver needed)
  - driver mode: `ibm_db_dbi` over TCP (optional dependency; imported lazily)

Counters from MON_GET_* are cumulative since database activation, so `collect_sampled` takes
snapshots and works on deltas.

Catalog collectors (`collect_static`, `collect_objects`) cover every user schema of the database. Results are
grouped by schema (`*_by_schema` dicts and a leading `schema` column); system schemas are skipped (`USER_SCHEMA_SQL`).
"""

import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import pandas as pd

SEP = "|"
_IDENT = re.compile(r"^[A-Za-z0-9_$#@.\-]+$")

# Where-fragment on a `schemaname` column: user schemas only (skips SYS*, IBM* and Db2 internal schemas)
USER_SCHEMA_SQL = ("schemaname not like 'SYS%' and schemaname not in ('NULLID','SQLJ','DB2GSE','IBM_RTMON','ADMIN') "
                   "and schemaname not like 'IBM%'")
# `<schema column> in (...)` subquery over the user schemas
_USER_SCHEMAS = f"in (select schemaname from syscat.schemata where {USER_SCHEMA_SQL})"


@dataclass
class Db2Source:
    database: str = "SAMPLE"
    schema: str = "DB2INST1"           # user schema scanned for tables
    container: str = ""                # docker mode when set
    instance_user: str = "db2inst1"
    host: str = ""                     # driver mode when container is empty
    port: int = 50000
    user: str = ""
    password: str = ""

    def validate(self) -> None:
        for label, value in (("database", self.database), ("schema", self.schema),
                             ("container", self.container), ("instance_user", self.instance_user)):
            if value and not _IDENT.match(value):
                raise ValueError(f"Invalid {label}: {value!r}")


class Db2Error(RuntimeError):
    pass


# ---------------------------------------------------------------- execution
def _run_docker(src: Db2Source, sql: str) -> List[List[str]]:
    script = f"db2 connect to {src.database} >/dev/null && db2 -x {shlex.quote(sql)}"
    cmd = ["docker", "exec", src.container, "su", "-", src.instance_user, "-c", script]
    proc = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=120)
    out = proc.stdout.strip()
    # db2 -x exit code 1 = "no rows"; >=2 = error
    if proc.returncode >= 2 or "SQL0" in out[:20] or "SQL1" in out[:20]:
        raise Db2Error((out or proc.stderr).strip()[:500])
    return [line.split(SEP) for line in out.splitlines() if line.strip()]


def _run_driver(src: Db2Source, sql: str) -> List[List[str]]:
    try:
        import ibm_db_dbi  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise Db2Error("ibm_db is not installed (pip install ibm_db) and no docker container was given") from exc
    dsn = (f"DATABASE={src.database};HOSTNAME={src.host};PORT={src.port};PROTOCOL=TCPIP;"
           f"UID={src.user};PWD={src.password};")
    conn = ibm_db_dbi.connect(dsn, "", "")
    try:
        cur = conn.cursor()
        cur.execute(sql)
        return [[("" if v is None else str(v)) for v in row] for row in cur.fetchall()]
    finally:
        conn.close()


def run_sql(src: Db2Source, exprs: List[str], tail: str) -> List[List[str]]:
    """Run `select <exprs joined by '|'> <tail>` and return rows split into string fields."""
    src.validate()
    joined = f"||'{SEP}'||".join(f"coalesce(rtrim(cast({e} as varchar(200))),'')" for e in exprs)
    sql = f"select {joined} {tail}"
    return _run_docker(src, sql) if src.container else _run_driver(src, sql)


def _f(value: str, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- static collection
def _grouped_counts(src: Db2Source, schemas: List[str],
                    specs: List[tuple]) -> Dict[str, Dict[str, int]]:
    """{schema: {key: count}} from one grouped query per spec (key, table, schema column, extra condition).
    Every schema in `schemas` gets every key, default 0."""
    keys = [spec[0] for spec in specs]
    out: Dict[str, Dict[str, int]] = {s: dict.fromkeys(keys, 0) for s in schemas}
    for key, table, col, extra in specs:
        rows = run_sql(src, [col, "count(*)"],
                       f"from {table} where {col} {_USER_SCHEMAS}{extra} group by {col}")
        for sch, n in rows:
            out.setdefault(sch, dict.fromkeys(keys, 0))[key] = int(_f(n))
    return out


def collect_static(src: Db2Source) -> Dict[str, Any]:
    """Point-in-time facts: host, sizes, config, inventory, migration risks. Covers all user schemas
    (src.schema is not used)."""
    out: Dict[str, Any] = {"database": src.database}

    r = run_sql(src, ["total_cpus", "total_memory", "os_name", "host_name"],
                "from table(sysproc.env_get_sys_info())")[0]
    out["host"] = {"cpus": _f(r[0]), "ram_gb": _f(r[1]) / 1024.0, "os": r[2], "hostname": r[3]}

    r = run_sql(src, ["service_level", "bld_level"], "from sysibmadm.env_inst_info")[0]
    out["version"] = r[0]

    # '' = all schemas (same convention as the tabname argument). If this call fails on a Db2 version,
    # fall back to one admin_get_tab_info(schema, '') call per user schema.
    tabs = run_sql(src, [
        "t.tabschema", "t.tabname", "coalesce(t.card,-1)", "coalesce(i.data_object_p_size,0)",
        "coalesce(i.index_object_p_size,0)",
        "coalesce(i.long_object_p_size,0)+coalesce(i.lob_object_p_size,0)+coalesce(i.xml_object_p_size,0)",
        "t.tableorg",
    ], f"from syscat.tables t left join table(sysproc.admin_get_tab_info('','')) i "
       f"on i.tabschema=t.tabschema and i.tabname=t.tabname "
       f"where t.type='T' and t.tabschema {_USER_SCHEMAS} order by coalesce(i.data_object_p_size,0) desc")
    tdf = pd.DataFrame(tabs, columns=["schema", "table", "rows", "data_kb", "index_kb", "lob_xml_kb",
                                      "organization"])
    for c in ("rows", "data_kb", "index_kb", "lob_xml_kb"):
        tdf[c] = pd.to_numeric(tdf[c], errors="coerce").fillna(0)
    tdf["total_kb"] = tdf["data_kb"] + tdf["index_kb"] + tdf["lob_xml_kb"]
    out["tables"] = tdf

    ts = run_sql(src, ["tbsp_name", "tbsp_page_size", "tbsp_total_pages", "tbsp_used_pages"],
                 "from table(mon_get_tablespace(null,-2))")
    sdf = pd.DataFrame(ts, columns=["tablespace", "page_size", "total_pages", "used_pages"])
    for c in ("page_size", "total_pages", "used_pages"):
        sdf[c] = pd.to_numeric(sdf[c], errors="coerce").fillna(0)
    sdf["allocated_mb"] = sdf["total_pages"] * sdf["page_size"] / 1048576
    sdf["used_mb"] = sdf["used_pages"] * sdf["page_size"] / 1048576
    out["tablespaces"] = sdf

    bp = run_sql(src, ["bpname", "npages", "pagesize"], "from syscat.bufferpools")
    out["bufferpool_gb"] = sum(_f(n) * _f(p) for _, n, p in bp if _f(n) > 0) / 1024 ** 3

    cfg = run_sql(src, ["name", "value"],
                  "from sysibmadm.dbcfg where name in ('logfilsiz','logprimary','logsecond','self_tuning_mem')")
    out["config"] = {k.strip(): v.strip() for k, v in cfg}

    schemas = list_schemas(src)
    cols = run_sql(src, ["tabschema", "typename", "count(*)"],
                   f"from syscat.columns where tabschema {_USER_SCHEMAS} group by tabschema, typename")
    col_types: Dict[str, Dict[str, int]] = {s: {} for s in schemas}
    for sch, typ, n in cols:
        col_types.setdefault(sch, {})[typ] = int(_f(n))
    out["column_types_by_schema"] = col_types

    out["counts_by_schema"] = _grouped_counts(src, schemas, [
        ("indexes", "syscat.indexes", "tabschema", ""),
        ("routines", "syscat.routines", "routineschema", ""),
        ("triggers", "syscat.triggers", "trigschema", ""),
        ("sequences", "syscat.sequences", "seqschema", ""),
    ])

    mdc = run_sql(src, ["tabschema", "tabname"], f"from syscat.indexes where tabschema {_USER_SCHEMAS} "
                                                 f"and indextype='BLOK' group by tabschema, tabname")
    mdc_by_schema: Dict[str, List[str]] = {s: [] for s in schemas}
    for sch, tab in mdc:
        mdc_by_schema.setdefault(sch, []).append(tab)
    out["mdc_by_schema"] = mdc_by_schema
    return out


# ---------------------------------------------------------------- object inventory
_ROUTINE_FLAGS = {  # Db2 SQL PL features that need manual rewrite for PL/pgSQL
    "cursor": "DECLARE%CURSOR",
    "handler": "HANDLER FOR",
    "dynamic_sql": "PREPARE ",
    "signal": "SIGNAL ",
    "diagnostics": "GET DIAGNOSTICS",
    "temp_table": "DECLARE GLOBAL TEMPORARY",
}


def _routine_effort(language: str, lines: float, flags: int) -> str:
    """Rough rewrite effort for a routine: High / Medium / Low."""
    if language.strip().upper() != "SQL" or flags >= 2 or lines > 200:
        return "High"
    if flags == 1 or lines > 30:
        return "Medium"
    return "Low"


def list_containers() -> List[str]:
    """Running docker containers whose image looks like Db2 (empty when docker is missing or stopped)."""
    try:
        proc = subprocess.run(["docker", "ps", "--format", "{{.Names}}|{{.Image}}"],
                              capture_output=True, encoding="utf-8", errors="replace", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    rows = [line.split("|", 1) for line in proc.stdout.splitlines() if "|" in line]
    return [name for name, image in rows if "db2" in image.lower() or "db2" in name.lower()]


def list_databases(src: Db2Source) -> List[str]:
    """Local databases cataloged in the Db2 instance (docker mode). Over TCP Db2 cannot list them: returns []."""
    if not src.container:
        return []
    src.validate()
    cmd = ["docker", "exec", src.container, "su", "-", src.instance_user, "-c", "db2 list db directory"]
    try:
        proc = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Db2Error(f"Cannot reach container '{src.container}': {exc}") from exc
    if proc.returncode >= 2 and not proc.stdout.strip():
        raise Db2Error((proc.stderr or "docker exec failed").strip()[:300])
    names, alias, local = [], None, False
    for line in proc.stdout.splitlines() + ["Database 0 entry:"]:
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if key == "Database alias":
            alias = val
        elif key == "Directory entry type":
            local = val.lower() == "indirect"
        elif line.startswith("Database ") and line.rstrip().endswith("entry:"):
            if alias and local and alias not in names:
                names.append(alias)
            alias, local = None, False
    return sorted(names)


def list_schemas(src: Db2Source) -> List[str]:
    """Non-system schemas that own at least one table, view or routine."""
    rows = run_sql(src, ["schemaname"],
                   f"from syscat.schemata where {USER_SCHEMA_SQL} order by schemaname")
    return [r[0].strip() for r in rows]


def collect_objects(src: Db2Source) -> Dict[str, Any]:
    """Inventory of all user schemas: tables (structure), views and routines (procedures / functions), and
    trigger / sequence counts per schema. Catalog queries only. Source text is not fetched, just its length
    and migration-relevant flags."""
    out: Dict[str, Any] = {}
    schemas = list_schemas(src)

    t = run_sql(src, [
        "t.tabschema", "t.tabname", "t.colcount", "coalesce(t.card,-1)", "t.tableorg",
        f"(select count(*) from syscat.keycoluse k join syscat.tabconst c on c.constname=k.constname and "
        f"c.tabschema=k.tabschema and c.tabname=k.tabname where c.tabschema=t.tabschema and c.tabname=t.tabname and c.type='P')",
        f"(select count(*) from syscat.tabconst c where c.tabschema=t.tabschema and c.tabname=t.tabname and c.type='F')",
        f"(select count(*) from syscat.indexes i where i.tabschema=t.tabschema and i.tabname=t.tabname)",
        f"(select count(*) from syscat.triggers g where g.tabschema=t.tabschema and g.tabname=t.tabname)",
    ], f"from syscat.tables t where t.type='T' and t.tabschema {_USER_SCHEMAS} "
       f"order by t.tabschema, t.tabname")
    tdf = pd.DataFrame(t, columns=["schema", "table", "columns", "rows", "organization", "pk_columns",
                                   "foreign_keys", "indexes", "triggers"])
    for c in ("columns", "rows", "pk_columns", "foreign_keys", "indexes", "triggers"):
        tdf[c] = pd.to_numeric(tdf[c], errors="coerce").fillna(0).astype(int)
    out["tables"] = tdf

    v = run_sql(src, [
        "v.viewschema", "v.viewname", "v.valid", "coalesce(length(v.text),0)",
        "(select count(*) from syscat.tabdep d where d.tabschema=v.viewschema and d.tabname=v.viewname)",
    ], f"from syscat.views v where v.viewschema {_USER_SCHEMAS} order by v.viewschema, v.viewname")
    vdf = pd.DataFrame(v, columns=["schema", "view", "valid", "text_length", "dependencies"])
    for c in ("text_length", "dependencies"):
        vdf[c] = pd.to_numeric(vdf[c], errors="coerce").fillna(0).astype(int)
    out["views"] = vdf

    body = "upper(cast(substr(r.text,1,32000) as varchar(32000)))"
    flag_exprs = [f"case when {body} like '%{pat}%' then 1 else 0 end" for pat in _ROUTINE_FLAGS.values()]
    r = run_sql(src, ["r.routineschema", "r.routinename", "r.routinetype", "r.language", "r.parm_count",
                      "coalesce(length(r.text),0)", *flag_exprs],
                f"from syscat.routines r where r.routineschema {_USER_SCHEMAS} and r.routinetype in ('P','F') "
                f"order by r.routineschema, r.routinename")
    rdf = pd.DataFrame(r, columns=["schema", "routine", "type", "language", "parameters", "text_length",
                                   *_ROUTINE_FLAGS])
    rdf["type"] = rdf["type"].map({"P": "Procedure", "F": "Function"}).fillna(rdf["type"])
    for c in ("parameters", "text_length", *_ROUTINE_FLAGS):
        rdf[c] = pd.to_numeric(rdf[c], errors="coerce").fillna(0).astype(int)
    rdf["db2_features"] = rdf[list(_ROUTINE_FLAGS)].sum(axis=1)
    # ~60 characters per line of SQL PL
    rdf["effort"] = [_routine_effort(l, n / 60.0, f) for l, n, f in
                     zip(rdf["language"], rdf["text_length"], rdf["db2_features"])]
    out["routines"] = rdf

    out["counts_by_schema"] = _grouped_counts(src, schemas, [
        ("triggers", "syscat.triggers", "trigschema", ""),
        ("sequences", "syscat.sequences", "seqschema", " and seqtype='S'"),
    ])
    return out


# ---------------------------------------------------------------- sampling
_DB_COLS = ["total_cpu_time", "total_app_commits", "total_app_rollbacks", "rows_read", "rows_returned",
            "direct_reads", "direct_writes", "log_disk_wait_time"]
_BP_KEYS = ["d_l", "d_p", "i_l", "i_p", "d_w", "i_w"]
_BP_COLS = ["pool_data_l_reads", "pool_data_p_reads", "pool_index_l_reads", "pool_index_p_reads",
            "pool_data_writes", "pool_index_writes"]


def _snapshot(src: Db2Source) -> Dict[str, float]:
    """One statement = one connection (one `docker exec`) per snapshot: database counters, summed
    bufferpool counters and the connection count are cross-joined (each side is a single row)."""
    exprs = [f"d.{c}" for c in _DB_COLS] + [f"b.{c}" for c in _BP_COLS] + ["c.n"]
    tail = ("from table(mon_get_database(-2)) d, "
            "(select " + ", ".join(f"sum({c}) as {c}" for c in _BP_COLS) +
            " from table(mon_get_bufferpool(null,-2))) b, "
            "(select count(*) as n from table(mon_get_connection(cast(null as bigint),-2))) c")
    r = run_sql(src, exprs, tail)[0]
    vals = [_f(v) for v in r]
    snap = dict(zip(_DB_COLS, vals[:len(_DB_COLS)]))
    snap.update(zip(_BP_KEYS, vals[len(_DB_COLS):len(_DB_COLS) + len(_BP_KEYS)]))
    snap["connections"] = vals[-1]
    snap["t"] = time.time()
    return snap


def parse_cpu_limit(cpu_max: str = "", cfs_quota: str = "", cfs_period: str = "") -> Optional[float]:
    """CPU limit in cores from cgroup v2 `cpu.max` ("<quota|max> <period>") or cgroup v1 quota/period."""
    try:
        if cpu_max.strip():
            quota, period = cpu_max.split()[:2]
            return None if quota == "max" else float(quota) / float(period)
        if cfs_quota.strip() and float(cfs_quota) > 0:
            return float(cfs_quota) / float(cfs_period)
    except (ValueError, ZeroDivisionError):
        pass
    return None


def detect_cpu_limit(src: Db2Source) -> Optional[float]:
    """Container CPU limit (cgroup) in cores, or None when unlimited / unknowable (driver mode)."""
    if not src.container:
        return None
    src.validate()
    script = ("cat /sys/fs/cgroup/cpu.max 2>/dev/null; echo '--'; "
              "cat /sys/fs/cgroup/cpu/cpu.cfs_quota_us 2>/dev/null; echo '--'; "
              "cat /sys/fs/cgroup/cpu/cpu.cfs_period_us 2>/dev/null")
    try:
        proc = subprocess.run(["docker", "exec", src.container, "sh", "-c", script],
                              capture_output=True, encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    parts = (proc.stdout.split("--") + ["", "", ""])[:3]
    return parse_cpu_limit(*parts)


def collect_sampled(src: Db2Source, cpus: float, interval_s: int = 30, samples: int = 2,
                    progress=None, duration_s: Optional[int] = None) -> pd.DataFrame:
    """Snapshot every `interval_s` and return one row of rates per interval.

    The window is `duration_s` when given (samples = ceil(duration_s / interval_s)), else
    `samples * interval_s`. CPU % uses the container CPU limit (cgroup) as denominator when one is
    set, otherwise `cpus` (host core count). Hit ratios are computed from counter deltas over the
    window, not lifetime totals.
    """
    if duration_s:
        samples = max(1, -(-int(duration_s) // max(int(interval_s), 1)))
    limit = detect_cpu_limit(src)
    denom = max(min(limit, cpus) if limit else cpus, 0.01)

    snaps = [_snapshot(src)]
    for i in range(samples):
        time.sleep(interval_s)
        snaps.append(_snapshot(src))
        if progress:
            progress((i + 1) / samples)
    rows = []
    bp_delta = {k: 0.0 for k in ("d_l", "d_p", "i_l", "i_p")}
    for i, (a, b) in enumerate(zip(snaps, snaps[1:])):
        dt = max(b["t"] - a["t"], 1e-6)
        d = {k: b[k] - a[k] for k in b if k not in ("t", "connections")}
        if any(v < 0 for v in d.values()):
            continue  # counters reset (database deactivated between snapshots): interval unusable
        for k in bp_delta:
            bp_delta[k] += d[k]
        phys_io = d["d_p"] + d["i_p"] + d["d_w"] + d["i_w"] + d["direct_reads"] + d["direct_writes"]
        rows.append({
            "sample": i,  # interval index (kept rows and dropped intervals stay aligned across databases)
            "interval_s": round(dt, 1),
            # total_cpu_time is microseconds of CPU
            "cpu_pct": 100.0 * (d["total_cpu_time"] / 1e6) / (dt * denom),
            "iops": phys_io / dt,
            "commits_per_s": d["total_app_commits"] / dt,
            "rollbacks_per_s": d["total_app_rollbacks"] / dt,
            "rows_read_per_s": d["rows_read"] / dt,
            "rows_returned_per_s": d["rows_returned"] / dt,
            "connections": max(b["connections"] - 1, 0),  # exclude this collector's own connection
            "log_wait_ms_per_s": d["log_disk_wait_time"] / dt,
        })
    df = pd.DataFrame(rows, columns=["sample", "interval_s", "cpu_pct", "iops", "commits_per_s", "rollbacks_per_s",
                                      "rows_read_per_s", "rows_returned_per_s", "connections",
                                      "log_wait_ms_per_s"])
    df["sample"] = df["sample"].astype(int)
    df.attrs["dropped_intervals"] = samples - len(rows)
    df.attrs["cpu_denominator"] = denom
    df.attrs["cpu_limit"] = limit
    # hit ratios over the sampled window (sum of per-interval deltas)
    bp = bp_delta
    df.attrs["data_hit_pct"] = 100.0 * (1 - bp["d_p"] / bp["d_l"]) if bp["d_l"] else None
    df.attrs["index_hit_pct"] = 100.0 * (1 - bp["i_p"] / bp["i_l"]) if bp["i_l"] else None
    return df


# ---------------------------------------------------------------- analysis
def analyze(static: Dict[str, Any], sampled: Optional[pd.DataFrame] = None,
            window_hours: Optional[float] = None, objects: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return {'recommender_kwargs': {...}, 'summary': {...}, 'findings': [...]} (pure, no I/O)."""
    findings: List[Dict[str, str]] = []

    def add(level: str, title: str, detail: str):
        findings.append({"level": level, "title": title, "detail": detail})

    host = static["host"]
    tdf: pd.DataFrame = static["tables"]
    sdf: pd.DataFrame = static["tablespaces"]
    data_gb = float(tdf["total_kb"].sum()) / 1024 ** 2
    alloc_gb = float(sdf["allocated_mb"].sum()) / 1024
    used_gb = float(sdf["used_mb"].sum()) / 1024
    disk_gb = max(alloc_gb, 0.001)

    kwargs: Dict[str, Any] = {
        "vcpus": host["cpus"], "ram_gb": host["ram_gb"], "disk_gb": disk_gb,
        "source_engine": "IBM Db2",
    }
    # working set proxy: bufferpool size (what Db2 chose to keep hot); never above user data + catalog in use
    if static.get("bufferpool_gb", 0) > 0:
        kwargs["working_set_gb"] = round(min(static["bufferpool_gb"], max(used_gb, 0.001)), 4)

    summary = {
        "db2_version": static.get("version"), "host_cpus": host["cpus"], "host_ram_gb": round(host["ram_gb"], 2),
        "user_data_gb": round(data_gb, 4), "allocated_gb": round(alloc_gb, 3), "used_gb": round(used_gb, 3),
        "tables": len(tdf), "indexes": static["counts"]["indexes"], "bufferpool_gb": round(static["bufferpool_gb"], 4),
    }

    if sampled is not None and sampled.attrs.get("dropped_intervals"):
        add("warning", f"{sampled.attrs['dropped_intervals']} sample interval(s) discarded",
            "Db2 restarted its counters between two readings (this happens when the database has no connected "
            "users and shuts down its in-memory state). Those intervals were ignored. Retry while applications "
            "are connected, or use a longer window so enough valid samples remain.")
    if sampled is not None and len(sampled):
        kwargs["peak_cpu_pct"] = round(float(sampled["cpu_pct"].max()), 2)
        kwargs["peak_iops"] = round(float(sampled["iops"].max()), 1)
        kwargs["client_connections"] = int(sampled["connections"].max())
        summary.update({"peak_cpu_pct": kwargs["peak_cpu_pct"], "peak_iops": kwargs["peak_iops"],
                        "p95_cpu_pct": round(float(sampled["cpu_pct"].quantile(0.95)), 2),
                        "p95_iops": round(float(sampled["iops"].quantile(0.95)), 1),
                        "samples": len(sampled),
                        "connections": kwargs["client_connections"],
                        "peak_commits_per_s": round(float(sampled["commits_per_s"].max()), 2)})
        span = float(sampled["interval_s"].sum())
        summary["window_s"] = round(span, 1)
        if sampled.attrs.get("cpu_limit"):
            add("info", f"CPU % uses container limit ({sampled.attrs['cpu_limit']:g} cores)",
                f"Host has {host['cpus']:g} cores but the container is capped; CPU % is relative to the cap.")
        if len(sampled) < 20:
            add("warning", "Few samples: P95 is close to max",
                f"Only {len(sampled)} interval(s); P95 is not meaningful below ~20 samples. Lengthen the window.")
        if span < 3600:
            add("warning", "Short sampling window",
                f"Metrics cover {span:.0f} s. Peak CPU/IOPS need a representative busy window (days, incl. month-end).")
        if sampled["log_wait_ms_per_s"].sum() > 0:
            add("info", "Log disk waits seen", "Size Azure WAL/storage IOPS with log write latency in mind.")
        for k, label in (("data_hit_pct", "data"), ("index_hit_pct", "index")):
            v = sampled.attrs.get(k)
            if v is not None:
                summary[k] = round(v, 1)
                if v < 90:
                    add("info", f"Low {label} bufferpool hit ratio ({v:.0f}%)",
                        "Likely cold cache after start-up; re-measure under steady load before sizing memory.")
    else:
        add("warning", "No sampled workload", "CPU, IOPS and connection peaks were not measured; recommender uses defaults.")

    stale = tdf[tdf["rows"] < 0]
    if len(stale):
        add("warning", f"{len(stale)} of {len(tdf)} tables have no statistics",
            "Db2 keeps row counts and column statistics only after someone runs RUNSTATS. These tables have never had it, "
            "so row counts show -1 and sizes are physical disk allocation, not row-based estimates. "
            "Ask the DBA to run RUNSTATS before relying on table sizes.")

    ct = static["column_types"]
    if ct.get("XML"):
        add("risk", f"{ct['XML']} XML column(s)", "Db2 stores XML in a special native format (pureXML). PostgreSQL has no equivalent, so these columns must be "
            "converted to the xml or jsonb type and any XQuery / SQL-XML code that reads them must be rewritten.")
    if ct.get("VECTOR"):
        add("risk", f"{ct['VECTOR']} VECTOR column(s)", "VECTOR columns store AI embeddings. PostgreSQL handles these with the pgvector extension, which you must enable "
            "on Azure Flexible Server. Similarity-search queries need to be re-checked.")
    lobs = ct.get("BLOB", 0) + ct.get("CLOB", 0) + ct.get("DBCLOB", 0)
    if lobs:
        add("info", f"{lobs} LOB column(s)", "LOBs are large binary or text values (files, documents). They map to bytea or text in PostgreSQL. Large LOBs "
            "increase storage and backup size, so confirm the real volume.")
    if ct.get("GRAPHIC") or ct.get("VARGRAPHIC"):
        add("info", "GRAPHIC/VARGRAPHIC columns", "These hold double-byte characters. PostgreSQL uses plain text in UTF-8, so check that the data converts "
            "correctly (character-set conversion).")
    mdc = static.get("mdc_tables", [])
    if mdc:
        add("risk", f"{len(mdc)} MDC table(s): {', '.join(mdc[:5])}",
            "MDC (multi-dimensional clustering) is a Db2-only way of physically grouping rows to speed up queries. "
            "PostgreSQL does not have it; replace it with table partitioning plus indexes and re-test query speed.")
    c = static["counts"]
    if c["routines"] or c["triggers"]:
        add("risk", f"{c['routines']} routines, {c['triggers']} triggers", "Stored procedures, functions and triggers are code that runs inside the database, written in Db2 SQL PL. "
            "They must be rewritten in PostgreSQL's PL/pgSQL. This is usually the biggest manual migration effort.")
    if c["sequences"]:
        add("info", f"{c['sequences']} sequences", "Sequences generate unique numbers (such as IDs). PostgreSQL has the same feature, so this is low risk. "
            "Just check that auto-numbered (identity) columns behave the same after migration.")
    if static["config"].get("self_tuning_mem", "").upper() == "OFF":
        add("info", "STMM off", "Self-tuning memory is off, so Db2 memory is set by hand. The buffer pool size (used here as a stand-in for "
            "the data kept in memory) may not match real usage, so treat the memory estimate with care.")

    if objects:
        rdf, vdf = objects["routines"], objects["views"]
        bad = vdf[vdf["valid"].str.upper() == "N"]
        if len(bad):
            add("warning", f"{len(bad)} invalid view(s)", f"Views Db2 marks as invalid: {', '.join(bad['view'][:5])}. "
                "They depend on dropped or changed objects and will fail on PostgreSQL too.")
        high = rdf[rdf["effort"] == "High"]
        if len(high):
            add("risk", f"{len(high)} high-effort routine(s)", "Routines that are external (Java/C), use several Db2-only "
                "SQL PL features, or are very long. Plan a manual rewrite: " + ", ".join(high["routine"][:5]))
        ext = rdf[rdf["language"].str.upper() != "SQL"]
        if len(ext):
            add("risk", f"{len(ext)} external routine(s)", "Routines written in a language other than SQL PL run outside "
                "the database engine. They need a new home (PL/pgSQL, or an application / Azure Function).")

    return {"recommender_kwargs": kwargs, "summary": summary, "findings": findings,
            "tables": tdf, "tablespaces": sdf, "sampled": sampled}
