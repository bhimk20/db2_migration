"""Scope selection for DB2 assessments.

Pure functions over the job result (no I/O, no Streamlit, no subprocess). They turn the
per-database, per-schema collections into the inputs that `core.db2_metrics.analyze` expects,
for one of three scopes:

  - schema:   one schema of one database
  - database: all user schemas of one database
  - instance: every readable database of the container (workload summed on a shared sample grid)

Job result shape:
  {"host": {...}, "taken": ts,
   "databases": {DB: {"static": dict|None, "objects": dict|None, "sampled": DataFrame|None, "error": str}}}
"""

from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd

from core.azure_recommender import recommend_options
from core.db2_metrics import analyze

SCOPES = ("schema", "database", "instance")

RATE_COLS = ("cpu_pct", "iops", "commits_per_s", "rollbacks_per_s", "rows_read_per_s",
             "rows_returned_per_s", "connections", "log_wait_ms_per_s")
_COUNT_KEYS = ("indexes", "routines", "triggers", "sequences")
_OBJECT_COLS = {
    "tables": ["schema", "table", "columns", "rows", "organization", "pk_columns", "foreign_keys",
               "indexes", "triggers"],
    "views": ["schema", "view", "valid", "text_length", "dependencies"],
    "routines": ["schema", "routine", "type", "language", "parameters", "text_length", "db2_features", "effort"],
}


def _upper(name: Any) -> str:
    return str(name or "").strip().upper()


def _by_upper(mapping: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {_upper(k): v for k, v in (mapping or {}).items()}


def _unreadable(database: str, entry: Dict[str, Any]) -> str:
    return f"Database {database!r} could not be read: {entry.get('error') or 'no data collected'}"


def _schemas_of(entry: Dict[str, Any]) -> Set[str]:
    """Upper-cased names of all schemas known for one database."""
    static = entry.get("static") or {}
    names: Set[str] = set()
    for key in ("counts_by_schema", "column_types_by_schema", "mdc_by_schema"):
        names.update(_upper(k) for k in (static.get(key) or {}))
    tables = static.get("tables")
    if tables is not None and "schema" in tables.columns:
        names.update(tables["schema"].map(_upper))
    return names


def _keep(df: Optional[pd.DataFrame], schemas: Optional[Set[str]]) -> Optional[pd.DataFrame]:
    """Rows of `df` in `schemas` (None = no filter). Frames without a schema column pass unchanged."""
    if df is None or schemas is None or "schema" not in df.columns:
        return df
    return df[df["schema"].map(_upper).isin(schemas)].copy()


def _slice(name: str, entry: Dict[str, Any], schemas: Optional[Set[str]]) -> Dict[str, Any]:
    """The part of one readable database covered by `schemas` (None = all user schemas).

    Tablespaces are database-wide, so for a schema slice they are scaled by the schema's share of
    the user data (estimate, not measured per schema).
    """
    static = entry["static"]
    objects = entry.get("objects") or {}
    tables = _keep(static["tables"], schemas)
    all_kb = float(static["tables"]["total_kb"].sum())
    kb = float(tables["total_kb"].sum())
    share = kb / all_kb if schemas is not None and all_kb > 0 else 1.0

    tablespaces = static["tablespaces"].copy()
    if schemas is not None:
        tablespaces["allocated_mb"] = tablespaces["allocated_mb"] * share
        tablespaces["used_mb"] = tablespaces["used_mb"] * share

    counts_by = _by_upper(static.get("counts_by_schema"))
    types_by = _by_upper(static.get("column_types_by_schema"))
    mdc_by = _by_upper(static.get("mdc_by_schema"))
    known = _schemas_of(entry)
    wanted = sorted(known if schemas is None else schemas & known)

    counts = {k: 0 for k in _COUNT_KEYS}
    column_types: Dict[str, int] = {}
    mdc: List[Tuple[str, str]] = []
    for s in wanted:
        for k in _COUNT_KEYS:
            counts[k] += int((counts_by.get(s) or {}).get(k, 0))
        for typename, n in (types_by.get(s) or {}).items():
            column_types[typename] = column_types.get(typename, 0) + int(n)
        mdc.extend((s, t) for t in (mdc_by.get(s) or []))

    frames = {}
    for key, cols in _OBJECT_COLS.items():
        df = objects.get(key)
        frames[key] = _keep(df, schemas) if df is not None else pd.DataFrame(columns=cols)

    return {
        "name": name, "host": static["host"], "version": static.get("version"),
        "config": static.get("config") or {}, "tables": tables, "tablespaces": tablespaces,
        "bufferpool_gb": float(static.get("bufferpool_gb") or 0.0), "counts": counts,
        "column_types": column_types, "mdc": mdc, "objects": frames, "sampled": entry.get("sampled"),
    }


def _combine_samples(samples: List[Optional[pd.DataFrame]],
                     selected: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    """Sum the workload of several databases on the `sample` grid they share.

    Only sample ids present in every database that has samples are kept. Rates are summed,
    interval_s is averaged. Hit ratios and dropped intervals come from the selected database.
    Returns None when no database has any rows.
    """
    usable = [s for s in samples if s is not None and len(s)]
    if not usable:
        return None
    keyed = []
    for s in usable:
        g = s.copy()
        if "sample" not in g.columns:
            g.insert(0, "sample", range(len(g)))
        keyed.append(g.set_index("sample"))
    ids = sorted(set.intersection(*(set(g.index) for g in keyed)))
    out = pd.DataFrame(index=pd.Index(ids, name="sample"))
    out["interval_s"] = pd.concat([g.loc[ids, "interval_s"] for g in keyed], axis=1).mean(axis=1)
    for col in RATE_COLS:
        out[col] = sum(g.loc[ids, col].astype(float) for g in keyed if col in g.columns)
    out = out.reset_index()[["sample", "interval_s", *RATE_COLS]]

    sel_attrs = selected.attrs if selected is not None else {}
    cpu_limit = next((s.attrs.get("cpu_limit") for s in usable if s.attrs.get("cpu_limit") is not None), None)
    out.attrs.update({
        "cpu_limit": cpu_limit,
        "cpu_denominator": sel_attrs.get("cpu_denominator"),
        "data_hit_pct": sel_attrs.get("data_hit_pct"),
        "index_hit_pct": sel_attrs.get("index_hit_pct"),
        "dropped_intervals": int(sel_attrs.get("dropped_intervals") or 0),
    })
    return out


def scope_view(result: Dict[str, Any], database: str, schema: str = "", scope: str = "database") -> Dict[str, Any]:
    """Run `analyze()` for one scope and return its output plus scope, label, databases and selected.

    schema: one schema of `database` (needs a schema name); database: all schemas of `database`;
    instance: every readable database of the container (`database` only selects the config and
    hit ratios). Raises ValueError for an unknown scope, database or schema, or an unreadable database.
    """
    if scope not in SCOPES:
        raise ValueError(f"Unknown scope {scope!r}; expected one of {', '.join(SCOPES)}")
    dbs: Dict[str, Dict[str, Any]] = result.get("databases") or {}
    if database not in dbs:
        raise ValueError(f"Database {database!r} is not part of this assessment")
    selected = dbs[database]
    schema_u = _upper(schema)
    included = [n for n, e in dbs.items() if e.get("static") is not None]

    if scope == "instance":
        if not included:
            raise ValueError("No database in this container could be read")
        slices = [_slice(n, dbs[n], None) for n in included]
        names = included
        label = f"All {len(included)} databases"
        objects_schema = ""
    else:
        if selected.get("static") is None:
            raise ValueError(_unreadable(database, selected))
        if scope == "schema":
            if not schema_u:
                raise ValueError("Schema scope needs a schema name")
            if schema_u not in _schemas_of(selected):
                raise ValueError(f"Schema {schema!r} is not in database {database!r}")
            slices = [_slice(database, selected, {schema_u})]
            label = f"{database} / {schema_u}"
            objects_schema = schema_u
        else:
            slices = [_slice(database, selected, None)]
            label = f"{database} (all schemas)"
            objects_schema = ""
        names = [database]

    primary = next((s for s in slices if s["name"] == database), slices[0])
    instance = scope == "instance"

    def _join(frames: List[Tuple[str, pd.DataFrame]]) -> pd.DataFrame:
        tagged = []
        for name, df in frames:
            df = df.copy()
            if instance:
                df.insert(0, "database", name)
            tagged.append(df)
        nonempty = [d for d in tagged if len(d)]
        return pd.concat(nonempty, ignore_index=True) if nonempty else tagged[0]

    def _mdc_label(name: str, schema_name: str, table: str) -> str:
        if instance:
            return f"{name}.{schema_name}.{table}"
        if scope == "database":
            return f"{schema_name}.{table}"
        return table

    counts = {k: sum(s["counts"][k] for s in slices) for k in _COUNT_KEYS}
    column_types: Dict[str, int] = {}
    for s in slices:
        for typename, n in s["column_types"].items():
            column_types[typename] = column_types.get(typename, 0) + n

    static = {
        "host": slices[0]["host"], "version": primary["version"], "config": primary["config"],
        "tables": _join([(s["name"], s["tables"]) for s in slices]),
        "tablespaces": _join([(s["name"], s["tablespaces"]) for s in slices]),
        "bufferpool_gb": sum(s["bufferpool_gb"] for s in slices),
        "counts": counts, "column_types": column_types,
        "mdc_tables": [_mdc_label(s["name"], sc, t) for s in slices for sc, t in s["mdc"]],
    }
    objects = {key: _join([(s["name"], s["objects"][key]) for s in slices]) for key in _OBJECT_COLS}
    objects["schema"] = objects_schema

    if instance:
        sampled = _combine_samples([s["sampled"] for s in slices],
                                   next((s["sampled"] for s in slices if s["name"] == database), None))
    else:
        sampled = slices[0]["sampled"]

    view = analyze(static, sampled, objects=objects)
    if instance:
        n = len(dbs)
        view["findings"].append({
            "level": "info",
            "title": f"{n} database{'s' if n != 1 else ''} share this container",
            "detail": ", ".join(dbs),
        })
        for name, entry in dbs.items():
            if entry.get("error"):
                view["findings"].append({
                    "level": "warning",
                    "title": f"Database {name} could not be read",
                    "detail": entry["error"],
                })
    view.update({"scope": scope, "label": label, "databases": names, "selected": database})
    return view


def db_breakdown(result: Dict[str, Any]) -> pd.DataFrame:
    """One row per database of the job result (`result["databases"]` as a dict).

    Sizes come from the tablespaces and tables, counts from the objects, peaks from the sampled
    workload (max of cpu_pct, iops and connections). Share columns (`*_share_pct`) are percent of
    the SUM of that column over all databases; the sum of per-database peaks is an upper bound of
    the true container peak, because the peaks may not occur at the same moment. Unknown values
    are NaN; a database that failed keeps its error text.
    """
    cols = ["database", "allocated_gb", "allocated_share_pct", "used_gb", "user_data_gb", "bufferpool_gb",
            "tables", "views", "routines", "peak_cpu_pct", "peak_cpu_share_pct", "peak_iops",
            "peak_iops_share_pct", "connections", "connections_share_pct", "error"]
    rows = []
    for name, entry in (result.get("databases") or {}).items():
        static = entry.get("static")
        row: Dict[str, Any] = {c: float("nan") for c in cols}
        row.update({"database": name, "error": entry.get("error") or ""})
        if static is not None:
            ts, tabs = static["tablespaces"], static["tables"]
            row["allocated_gb"] = float(ts["allocated_mb"].sum()) / 1024
            row["used_gb"] = float(ts["used_mb"].sum()) / 1024
            row["user_data_gb"] = float(tabs["total_kb"].sum()) / 1024 ** 2
            row["bufferpool_gb"] = float(static.get("bufferpool_gb") or 0.0)
            objects = entry.get("objects") or {}
            for key, col in (("tables", "tables"), ("views", "views"), ("routines", "routines")):
                if objects.get(key) is not None:
                    row[col] = float(len(objects[key]))
        sampled = entry.get("sampled")
        if sampled is not None and len(sampled):
            row["peak_cpu_pct"] = float(sampled["cpu_pct"].max())
            row["peak_iops"] = float(sampled["iops"].max())
            row["connections"] = float(sampled["connections"].max())
        rows.append(row)
    df = pd.DataFrame(rows, columns=cols)
    for src, share in (("allocated_gb", "allocated_share_pct"), ("peak_cpu_pct", "peak_cpu_share_pct"),
                       ("peak_iops", "peak_iops_share_pct"), ("connections", "connections_share_pct")):
        total = df[src].sum()
        df[share] = 100 * df[src] / total if total else float("nan")
    for c in ("tables", "views", "routines", "connections"):
        df[c] = df[c].astype("Int64")
    return df


def database_recommendations(result: Dict[str, Any], is_production: bool = False) -> List[Dict[str, Any]]:
    """One entry per database: its own sizing options, from its own static data and sampled workload.

    Each entry is {"database", "error", "options"}; options is recommend_options() output, or None
    when the database could not be read or sized (error holds why). One bad database never stops the rest.
    """
    out = []
    for name, entry in (result.get("databases") or {}).items():
        item: Dict[str, Any] = {"database": name, "error": entry.get("error") or "", "options": None}
        if entry.get("static") is not None:
            try:
                kw = scope_view(result, name, "", "database")["recommender_kwargs"]
                item["options"] = recommend_options(is_production=is_production, **kw)
            except Exception as exc:
                item["error"] = str(exc) or type(exc).__name__
        out.append(item)
    return out


def scope_objects(objects_by_db: Dict[str, Optional[Dict[str, Any]]], database: str,
                  schema: str = "") -> Optional[Dict[str, Any]]:
    """Object inventory of one database for the explorer, filtered by schema ("" = all schemas).

    Returns None when the database has not been read yet. Trigger and sequence counts come from
    `counts_by_schema` of the kept schemas.
    """
    objs = (objects_by_db or {}).get(database)
    if not objs:
        return None
    schema_u = _upper(schema)

    def _pick(df: Optional[pd.DataFrame]) -> pd.DataFrame:
        if df is None:
            return pd.DataFrame()
        if not schema_u or "schema" not in df.columns:
            return df.copy()
        return df[df["schema"].map(_upper) == schema_u].copy()

    counts_by = _by_upper(objs.get("counts_by_schema"))
    keys = [schema_u] if schema_u else list(counts_by)
    return {
        "database": database,
        "schema": schema_u,
        "tables": _pick(objs.get("tables")),
        "views": _pick(objs.get("views")),
        "routines": _pick(objs.get("routines")),
        "counts": {
            "triggers": sum(int((counts_by.get(k) or {}).get("triggers", 0)) for k in keys),
            "sequences": sum(int((counts_by.get(k) or {}).get("sequences", 0)) for k in keys),
        },
    }
