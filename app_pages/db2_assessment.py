"""
DB2 assessment page: collect sizing metrics from a Db2 database, analyze them and feed the Azure recommender.
"""

from collections import Counter

import pandas as pd
import streamlit as st

from core.azure_recommender import recommend_azure_postgresql
from core.dataset_loader import convert_df_to_excel_bytes
from app_pages.db2_connection import connection_form
from core.db2_jobs import get_job
from core.db2_scope import database_recommendations, db_breakdown, scope_view

SCOPE_KEYS = {"Schema": "schema", "Database": "database", "All databases": "instance"}


def _size(gb: float) -> str:
    return f"{gb * 1024:,.1f} MB" if gb < 1 else f"{gb:,.1f} GB" if gb < 1024 else f"{gb / 1024:,.2f} TB"


def _series(sampled: pd.DataFrame, cols: dict) -> pd.DataFrame:
    """Sampled rates against elapsed seconds, with readable column names for the chart legend."""
    out = pd.DataFrame({"Elapsed (s)": sampled["interval_s"].cumsum().to_numpy()})
    for label, col in cols.items():
        out[label] = sampled[col].to_numpy()
    return out


def _by_database(frames: dict, col: str) -> pd.DataFrame:
    """One column per database, indexed by the sample id; samples missing in one database count as 0."""
    parts = []
    for name, s in frames.items():
        index = s["sample"].to_numpy() if "sample" in s.columns else range(len(s))
        parts.append(pd.Series(s[col].to_numpy(), index=index, name=name))
    return pd.concat(parts, axis=1).fillna(0).sort_index().rename_axis("Sample")


def _table_labels(df: pd.DataFrame, scope: str) -> pd.Series:
    """`database.schema.table` for the container scope, `schema.table` otherwise (only the columns present)."""
    parts = ["database"] if scope == "instance" else []
    parts += ["schema", "table"]
    return df[[p for p in parts if p in df.columns]].astype(str).apply(".".join, axis=1)


st.title("DB2 assessment")
st.caption("Read-only queries against a Db2 database. Results feed the Azure PostgreSQL sizing engine. "
           "Nothing is changed on the database.")
with st.expander("New here? How this page works"):
    st.markdown(
        "1. **Connect** in the sidebar (a Docker container, or over the network), then pick the database and schema from the dropdowns.\n"
        "2. **It assesses in the background** once you connect, and keeps the result. The page reads facts that are stored in the database (sizes, tables, settings) "
        "and then *watches* it for a while to measure how busy it is.\n"
        "3. **Read the results:** the decision card at the top is the Azure PostgreSQL size we suggest. Use **Size for** there to size one schema, "
        "the whole database (the default) or every database in the container. "
        "The tabs below show the databases, workload, storage and findings.\n\n"
        "Why watch for a while? Db2 only keeps running totals (like a car's odometer), not history. To get a "
        "rate such as *operations per second*, the page reads the totals, waits, reads them again and divides.")

src = connection_form()
with st.sidebar:
    with st.expander("Sampling settings", icon=":material/tune:"):
        sample_on = st.toggle("Sample workload", value=True, key="db2_sample_on", persist_state="session",
                              help="Measures how busy the database is (CPU, disk operations, connections) by reading Db2's "
                                   "running totals repeatedly. It does NOT add any load. Turn it off to get only sizes and findings.")
        s2, s3 = st.columns(2)
        interval = s2.number_input("Seconds per sample", min_value=5, max_value=3600, value=30, step=5, key="db2_interval",
                                   persist_state="session", disabled=not sample_on,
                                   help="Gap between two readings. Short gaps catch brief spikes; long gaps average them out.")
        window = s3.number_input("Sampling window (seconds)", min_value=10, max_value=86400, value=60, step=10, key="db2_window",
                                 persist_state="session", disabled=not sample_on,
                                 help="Total time to sample. Number of samples = window / seconds per sample.")
        n_samples = -(-int(window) // int(interval))
        if sample_on and n_samples < 10:
            st.caption(f":material/info: {n_samples} sample(s). Use 20 or more for a meaningful P95.")
        st.caption("Applied the next time you press **Re-run assessment** above. Nothing is re-collected while you change these.")

if src is None:
    st.info("Connect to a database in the sidebar.", icon=":material/arrow_back:")
    st.stop()

job = get_job(src)
res = job.result if job else None
if not res:
    if not (job and job.status == "error"):
        st.info("The assessment is running in the background. Results appear here when it finishes.", icon=":material/hourglass_top:")
    st.stop()

icons = {"risk": ":material/error:", "warning": ":material/warning:", "info": ":material/info:"}

# Decision card first. The scope switch and the production toggle sit inside the card; a rerun here is cheap.
st.subheader("Azure PostgreSQL recommendation")
with st.container(border=True):
    scope_choice = st.segmented_control("Size for", list(SCOPE_KEYS), default="Database", key="db2_scope",
                                        persist_state="session", required=True)
    scope = SCOPE_KEYS.get(scope_choice, "database")
    if scope == "schema" and not src.schema:
        st.caption("Pick a schema in the sidebar to size a single schema.")
        scope = "database"
    try:
        view = scope_view(res, src.database, src.schema, scope)
    except ValueError as exc:
        st.error(str(exc))
        st.stop()
    st.caption(view["label"])

    is_prod = st.toggle("Production workload", value=False,
                        help="Switch on to get a high-availability (zone-redundant) setup.")
    kw = dict(view["recommender_kwargs"])
    rec = recommend_azure_postgresql(is_production=is_prod, **kw)
    sku = rec["recommended_sku"]
    st.markdown(f"#### `{sku['sku']}`")
    r = st.columns(4)
    r[0].metric("Tier", sku["tier"])
    r[1].metric("vCores", sku["vcpus"])
    r[2].metric("Memory", f"{sku['memory_gib']} GiB")
    r[3].metric("Storage", f"{rec['target_storage_gib']:,} GiB")
    st.caption(f"HA: {'zone-redundant' if rec['ha_enabled'] else 'standalone'}. "
               "A starting point from the measured CPU, memory, disk and IOPS.")

st.markdown("**Per database (each database gets its own Azure server)**")
st.caption("Three options per database: the same sizing at a different CPU target. "
           "Target peak CPU is how busy the Azure server may get at the observed peak: conservative = 50% (most headroom, "
           "more vCores), balanced = 65% (the default above), lean = 80% (least headroom, fewer vCores).")
rows = []
for item in database_recommendations(res, is_prod):
    if item["options"] is None:
        rows.append({"Database": item["database"], "Option": "", "Error": item["error"]})
        continue
    for label, opt in item["options"].items():
        r_, s_ = opt["rec"], opt["rec"]["recommended_sku"]
        rows.append({"Database": item["database"], "Option": label, "Target peak CPU": f"{opt['target_cpu_utilization']:.0%}",
                     "SKU": s_["sku"], "vCores": s_["vcpus"], "RAM (GiB)": s_["memory_gib"],
                     "Storage (GiB)": r_["target_storage_gib"], "IOPS": r_["effective_iops"], "Error": ""})
options_df = pd.DataFrame(rows)
st.dataframe(options_df, hide_index=True, alt="Azure sizing options per database")

sm = view["summary"]
sampled = view["sampled"]
has_samples = sampled is not None and len(sampled) > 0
findings = view["findings"]
breakdown = db_breakdown(res)

t_over, t_db, t_work, t_stor, t_find = st.tabs(["Overview", "Databases", "Workload", "Storage", "Findings"])

with t_over:
    m = st.columns(4)
    m[0].metric("Db2", sm["db2_version"], help="Version of the source database.")
    m[1].metric("Host", f"{sm['host_cpus']:.0f} CPU / {sm['host_ram_gb']:.1f} GB",
                help="CPU cores and memory of the machine (or container host) running Db2.")
    m[2].metric("User data", _size(sm["user_data_gb"]), f"{sm['tables']} tables, {sm['indexes']} indexes", delta_color="off", delta_arrow="off",
                help="Real data stored in the tables of the chosen scope (data + indexes + large objects), without free space.")
    m[3].metric("Allocated", _size(sm["allocated_gb"]), f"{_size(sm['used_gb'])} used", delta_color="off", delta_arrow="off",
                help="Disk space Db2 has reserved for the database (tablespaces). Usually bigger than the data itself. "
                     "This drives the storage size we recommend.")
    if "peak_cpu_pct" in sm:
        w = st.columns(4)
        w[0].metric("Peak CPU", f"{sm['peak_cpu_pct']:.2f} %", f"P95 {sm['p95_cpu_pct']:.2f} %", delta_color="off", delta_arrow="off",
                    help="Highest CPU use seen across all samples. P95 below it means: 95% of the samples were at or under that value. "
                         "P95 is a fairer 'busy' level because it ignores a single freak spike.")
        w[1].metric("Peak IOPS", f"{sm['peak_iops']:,.1f}", f"P95 {sm['p95_iops']:,.1f}", delta_color="off", delta_arrow="off",
                    help="IOPS = disk reads and writes per second. Azure storage tiers are priced and limited by IOPS, "
                         "so this helps pick the right disk.")
        w[2].metric("Connections", sm["connections"],
                    help="Most applications connected at the same time (the page's own connection is excluded).")
        w[3].metric("Peak commits/s", f"{sm['peak_commits_per_s']:.2f}",
                    help="Transactions saved per second. A simple measure of how write-heavy the workload is.")
    if sm.get("samples", 0) < 10:
        st.warning(f"Only {sm.get('samples', 0)} sample(s) were taken. Sizing for production needs metrics over a "
                   "representative busy period.", icon=":material/schedule:")

    counts = Counter(f["level"] for f in findings)
    with st.container(horizontal=True):
        st.badge(f"{counts.get('risk', 0)} risk(s)", icon=":material/error:", color="red")
        st.badge(f"{counts.get('warning', 0)} warning(s)", icon=":material/warning:", color="orange")
        st.badge(f"{counts.get('info', 0)} info", icon=":material/info:", color="blue")
    st.caption("See the Findings tab for details.")

    with st.expander("More measurements"):
        x = st.columns(4)
        x[0].metric("Sampled window", f"{sm['window_s']:.0f} s" if "window_s" in sm else "n/a",
                    f"{sm['samples']} samples" if "samples" in sm else None, delta_color="off", delta_arrow="off",
                    help="How long the page actually measured. The longer and the busier the period, the more you can trust the peaks.")
        x[1].metric("Data hit ratio", f"{sm['data_hit_pct']:.1f} %" if "data_hit_pct" in sm else "n/a",
                    help="Share of data reads served from memory instead of disk. Near 100% = memory is big enough. "
                         "Low (under ~90%) can mean more memory is needed, or the cache was still cold.")
        x[2].metric("Index hit ratio", f"{sm['index_hit_pct']:.1f} %" if "index_hit_pct" in sm else "n/a",
                    help="Same as the data hit ratio, but for indexes (lookup structures).")
        cap = sampled.attrs.get("cpu_limit") if sampled is not None else None
        x[3].metric("CPU % basis", f"{cap:g} cores (cap)" if cap else f"{sm['host_cpus']:.0f} cores (host)",
                    help="CPU % is measured against this many cores. If the container is capped, the cap is used; otherwise all host cores.")

    with st.expander("Inputs passed to the recommender"):
        st.json(kw)

    export = {
        "Summary": pd.DataFrame([{"Metric": k, "Value": v} for k, v in sm.items()]),
        "Sizing options": options_df,
        "Findings": pd.DataFrame(findings),
        "Tables": view["tables"],
        "Tablespaces": view["tablespaces"],
        "Recommender inputs": pd.DataFrame([{"Input": k, "Value": v} for k, v in kw.items()]),
    }
    if has_samples:
        export["Samples"] = sampled
    export["Databases"] = breakdown
    st.download_button("Download DB2 analysis (.xlsx)", data=lambda: convert_df_to_excel_bytes(export), icon=":material/download:",
                       file_name=f"db2_{src.database}_{src.schema or 'all'}_{scope}_analysis.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

with t_db:
    measure = st.segmented_control("Measure", ["Allocated", "Used"], default="Allocated", key="db2_pie_measure", required=True)
    measure = measure or "Allocated"
    col = "used_gb" if measure == "Used" else "allocated_gb"
    pie = breakdown.loc[breakdown[col] > 0, ["database", col]].rename(columns={col: "value"}).reset_index(drop=True)
    pie["legend"] = pie["database"].where(pie["database"] != src.database, pie["database"] + " (selected)")
    pie["share"] = pie["value"] / pie["value"].sum() if len(pie) else pd.Series(dtype=float)
    unit = f"{measure.lower()} (GB)"
    with st.container(border=True):
        st.markdown(f"**Storage per database ({measure.lower()})**")
        if len(pie):
            spec = {
                "mark": {"type": "arc", "innerRadius": 60},
                "encoding": {
                    "theta": {"field": "value", "type": "quantitative", "title": unit},
                    "color": {"field": "legend", "type": "nominal", "title": "Database"},
                    "tooltip": [
                        {"field": "database", "type": "nominal", "title": "Database"},
                        {"field": "value", "type": "quantitative", "title": unit, "format": ",.2f"},
                        {"field": "share", "type": "quantitative", "title": "Share", "format": ".1%"},
                    ],
                },
            }
            st.vega_lite_chart(pie, spec, alt=f"Donut chart of {measure.lower()} storage per database in the container")
            st.caption(f"Container total: {pie['value'].sum():,.2f} GB {measure.lower()} across {len(pie)} database(s).")
        else:
            st.caption("No storage to show for this measure.")
    st.caption("One row per database. Shares are percent of the container total. Container peak CPU, IOPS and connections "
               "are the sum of per-database peaks: an upper bound, since peaks may not coincide. "
               "A database that could not be read keeps its error text.")
    st.dataframe(
        breakdown, hide_index=True, alt="Storage, objects and peak workload per database",
        column_config={
            "database": st.column_config.TextColumn("Database"),
            "allocated_gb": st.column_config.NumberColumn("Allocated (GB)", format="%.2f"),
            "used_gb": st.column_config.NumberColumn("Used (GB)", format="%.2f"),
            "allocated_share_pct": st.column_config.NumberColumn("Allocated share (%)", format="%.1f"),
            "user_data_gb": st.column_config.NumberColumn("User data (GB)", format="%.3f"),
            "bufferpool_gb": st.column_config.NumberColumn("Bufferpool (GB)", format="%.2f"),
            "tables": st.column_config.NumberColumn("Tables", format="%d"),
            "views": st.column_config.NumberColumn("Views", format="%d"),
            "routines": st.column_config.NumberColumn("Routines", format="%d"),
            "peak_cpu_pct": st.column_config.NumberColumn("Peak CPU (%)", format="%.2f"),
            "peak_cpu_share_pct": st.column_config.NumberColumn("CPU share (%)", format="%.1f"),
            "peak_iops": st.column_config.NumberColumn("Peak IOPS", format="%.1f"),
            "peak_iops_share_pct": st.column_config.NumberColumn("IOPS share (%)", format="%.1f"),
            "connections": st.column_config.NumberColumn("Connections", format="%d"),
            "connections_share_pct": st.column_config.NumberColumn("Connections share (%)", format="%.1f"),
            "error": st.column_config.TextColumn("Error"),
        })

with t_work:
    st.caption("Workload is measured per database. Schema and database scope use the selected database; "
               "All databases adds every database.")
    if not has_samples or "p95_cpu_pct" not in sm:
        st.caption("No usable samples. Turn on workload sampling in the sidebar and re-run the assessment.")
    else:
        st.caption(f"P95 CPU {sm['p95_cpu_pct']:.2f} %, P95 IOPS {sm['p95_iops']:,.1f}, "
                   f"peak commits/s {sm['peak_commits_per_s']:.2f}. Each point is one sampling interval.")
        g1, g2 = st.columns(2)
        with g1, st.container(border=True):
            st.markdown("**CPU % over time**")
            st.line_chart(_series(sampled, {"CPU %": "cpu_pct"}), x="Elapsed (s)", y="CPU %",
                          alt="CPU use over the sampling window, with peaks")
        with g2, st.container(border=True):
            st.markdown("**IOPS over time**")
            st.area_chart(_series(sampled, {"IOPS": "iops"}), x="Elapsed (s)", y="IOPS",
                          alt="Disk operations per second over the sampling window")
        g3, g4 = st.columns(2)
        with g3, st.container(border=True):
            st.markdown("**Commits and rollbacks per second**")
            st.line_chart(_series(sampled, {"Commits/s": "commits_per_s", "Rollbacks/s": "rollbacks_per_s"}),
                          x="Elapsed (s)", y=["Commits/s", "Rollbacks/s"],
                          alt="Commits and rollbacks per second over the sampling window")
        with g4, st.container(border=True):
            st.markdown("**Connections over time**")
            st.line_chart(_series(sampled, {"Connections": "connections"}), x="Elapsed (s)", y="Connections",
                          alt="Applications connected to the database over the sampling window")
        if scope == "instance":
            per_db = {name: e["sampled"] for name, e in res["databases"].items()
                      if e.get("sampled") is not None and len(e["sampled"])}
            if len(per_db) >= 2:
                with st.container(border=True):
                    st.markdown("**CPU % by database (stacked)**")
                    st.area_chart(_by_database(per_db, "cpu_pct"), stack=True,
                                  alt="CPU use per database, stacked, over the sampling window")
                with st.container(border=True):
                    st.markdown("**IOPS by database (stacked)**")
                    st.area_chart(_by_database(per_db, "iops"), stack=True,
                                  alt="Disk operations per second per database, stacked, over the sampling window")
        h = st.columns(2)
        h[0].metric("Data hit ratio", f"{sm['data_hit_pct']:.1f} %" if "data_hit_pct" in sm else "n/a")
        h[1].metric("Index hit ratio", f"{sm['index_hit_pct']:.1f} %" if "index_hit_pct" in sm else "n/a")
        st.markdown("**Samples**")
        st.caption("One row per reading interval: the rates measured between two consecutive readings.")
        st.dataframe(sampled, hide_index=True, alt="Rates measured per sampling interval")

with t_stor:
    tables = view["tables"]
    if len(tables):
        top = tables.nlargest(10, "total_kb").reset_index(drop=True)
        size_mb = pd.DataFrame({
            "Data": (top["data_kb"] / 1024).to_numpy(),
            "Indexes": (top["index_kb"] / 1024).to_numpy(),
            "LOB and XML": (top["lob_xml_kb"] / 1024).to_numpy(),
        }, index=_table_labels(top, scope).to_numpy())
        with st.container(border=True):
            st.markdown("**Largest tables by size (MB)**")
            st.bar_chart(size_mb, horizontal=True, stack=True,
                         alt="Ten largest tables by size, split into data, indexes and large objects")
    else:
        st.caption("No tables in this scope.")
    st.dataframe(tables, hide_index=True, alt="Size per table in KB")
    st.caption("Size per table in KB. 'rows' = -1 means Db2 has no statistics for it (RUNSTATS never ran).")

    tablespaces = view["tablespaces"]
    if len(tablespaces):
        names = tablespaces["tablespace"].astype(str)
        if scope == "instance" and "database" in tablespaces.columns:
            names = tablespaces["database"].astype(str) + " / " + names
        ts = pd.DataFrame({
            "Allocated": tablespaces["allocated_mb"].to_numpy(),
            "Used": tablespaces["used_mb"].to_numpy(),
        }, index=names.to_numpy())
        with st.container(border=True):
            st.markdown("**Tablespaces: allocated vs used (MB)**")
            st.bar_chart(ts, horizontal=True, stack=False,
                         alt="Allocated and used disk space per tablespace, in MB")
    st.dataframe(tablespaces, hide_index=True, alt="Allocated and used space per tablespace")
    st.caption("Tablespaces are the disk areas Db2 stores tables in. Allocated = reserved, used = filled. "
               "In the container scope the database is shown in front of the tablespace name.")

with t_find:
    with st.expander("What the levels mean"):
        st.markdown(":material/error: **Risk** = needs rework before or during migration. "
                    ":material/warning: **Warning** = may make the sizing unreliable. "
                    ":material/info: **Info** = good to know, usually low effort.")
    by_level = {lvl: [f for f in findings if f["level"] == lvl] for lvl in ["risk", "warning", "info"]}
    for lvl, label in [("risk", "Risks"), ("warning", "Warnings")]:
        items = by_level[lvl]
        st.subheader(f"{label} ({len(items)})")
        if not items:
            st.caption("None.")
        for f in items:
            with st.container(border=True):
                st.markdown(f"{icons[lvl]} **{f['title']}**")
                st.caption(f["detail"])
    if by_level["info"]:
        with st.expander(f"Good to know ({len(by_level['info'])})"):
            for f in by_level["info"]:
                with st.container(border=True):
                    st.markdown(f"{icons['info']} **{f['title']}**")
                    st.caption(f["detail"])
