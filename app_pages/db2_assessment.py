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

def _size(gb: float) -> str:
    return f"{gb * 1024:,.1f} MB" if gb < 1 else f"{gb:,.1f} GB" if gb < 1024 else f"{gb / 1024:,.2f} TB"


def _series(sampled: pd.DataFrame, cols: dict) -> pd.DataFrame:
    """Sampled rates against elapsed seconds, with readable column names for the chart legend."""
    out = pd.DataFrame({"Elapsed (s)": sampled["interval_s"].cumsum().to_numpy()})
    for label, col in cols.items():
        out[label] = sampled[col].to_numpy()
    return out


st.title("DB2 assessment")
st.caption("Read-only queries against a Db2 database. Results feed the Azure PostgreSQL sizing engine. "
           "Nothing is changed on the database.")
with st.expander("New here? How this page works"):
    st.markdown(
        "1. **Connect** in the sidebar (a Docker container, or over the network), then pick the database and schema from the dropdowns.\n"
        "2. **It assesses in the background** as soon as you pick a schema, and keeps the result. The page reads facts that are stored in the database (sizes, tables, settings) "
        "and then *watches* it for a while to measure how busy it is.\n"
        "3. **Read the results:** the decision card at the top is the Azure PostgreSQL size we suggest. "
        "The tabs below show the workload, storage, findings and raw data.\n\n"
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

sm = res["summary"]
sampled = res["sampled"]
has_samples = sampled is not None and len(sampled) > 0
icons = {"risk": ":material/error:", "warning": ":material/warning:", "info": ":material/info:"}

# Decision card first. The production toggle sits inside the card; a rerun here is cheap.
st.subheader("Azure PostgreSQL recommendation")
with st.container(border=True):
    is_prod = st.toggle("Production workload", value=False,
                        help="Switch on to get a high-availability (zone-redundant) setup.")
    kw = dict(res["recommender_kwargs"])
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

t_over, t_work, t_stor, t_find, t_data = st.tabs(["Overview", "Workload", "Storage", "Findings", "Data"])

with t_over:
    m = st.columns(4)
    m[0].metric("Db2", sm["db2_version"], help="Version of the source database.")
    m[1].metric("Host", f"{sm['host_cpus']:.0f} CPU / {sm['host_ram_gb']:.1f} GB",
                help="CPU cores and memory of the machine (or container host) running Db2.")
    m[2].metric("User data", _size(sm["user_data_gb"]), f"{sm['tables']} tables, {sm['indexes']} indexes", delta_color="off", delta_arrow="off",
                help="Real data stored in the tables of the chosen schema (data + indexes + large objects), without free space.")
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

    counts = Counter(f["level"] for f in res["findings"])
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

with t_work:
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
        h = st.columns(2)
        h[0].metric("Data hit ratio", f"{sm['data_hit_pct']:.1f} %" if "data_hit_pct" in sm else "n/a")
        h[1].metric("Index hit ratio", f"{sm['index_hit_pct']:.1f} %" if "index_hit_pct" in sm else "n/a")

with t_stor:
    if len(res["tables"]):
        top = res["tables"].nlargest(10, "total_kb")
        size_mb = pd.DataFrame({
            "Data": (top["data_kb"] / 1024).to_numpy(),
            "Indexes": (top["index_kb"] / 1024).to_numpy(),
            "LOB and XML": (top["lob_xml_kb"] / 1024).to_numpy(),
        }, index=top["table"].to_numpy())
        with st.container(border=True):
            st.markdown("**Largest tables by size (MB)**")
            st.bar_chart(size_mb, horizontal=True, stack=True,
                         alt="Ten largest tables by size, split into data, indexes and large objects")
    else:
        st.caption("No tables in the chosen schema.")
    if len(res["tablespaces"]):
        ts = pd.DataFrame({
            "Allocated": res["tablespaces"]["allocated_mb"].to_numpy(),
            "Used": res["tablespaces"]["used_mb"].to_numpy(),
        }, index=res["tablespaces"]["tablespace"].to_numpy())
        with st.container(border=True):
            st.markdown("**Tablespaces: allocated vs used (MB)**")
            st.bar_chart(ts, horizontal=True, stack=False,
                         alt="Allocated and used disk space per tablespace, in MB")

with t_find:
    with st.expander("What the levels mean"):
        st.markdown(":material/error: **Risk** = needs rework before or during migration. "
                    ":material/warning: **Warning** = may make the sizing unreliable. "
                    ":material/info: **Info** = good to know, usually low effort.")
    by_level = {lvl: [f for f in res["findings"] if f["level"] == lvl] for lvl in ["risk", "warning", "info"]}
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

with t_data:
    with st.expander("Tables"):
        st.caption("Size per table in KB. 'rows' = -1 means Db2 has no statistics for it (RUNSTATS never ran).")
        st.dataframe(res["tables"], hide_index=True, alt="Size per table in KB")
    with st.expander("Tablespaces"):
        st.caption("Tablespaces are the disk areas Db2 stores tables in. Allocated = reserved, used = filled.")
        st.dataframe(res["tablespaces"], hide_index=True, alt="Allocated and used space per tablespace")
    with st.expander("Samples"):
        if has_samples:
            st.caption("One row per reading interval: the rates measured between two consecutive readings.")
            st.dataframe(sampled, hide_index=True, alt="Rates measured per sampling interval")
        else:
            st.caption("No usable samples.")
    with st.expander("Inputs passed to the recommender"):
        st.json(kw)

    export = {
        "Summary": pd.DataFrame([{"Metric": k, "Value": v} for k, v in sm.items()]),
        "Findings": pd.DataFrame(res["findings"]),
        "Tables": res["tables"],
        "Tablespaces": res["tablespaces"],
        "Recommender inputs": pd.DataFrame([{"Input": k, "Value": v} for k, v in kw.items()]),
    }
    if has_samples:
        export["Samples"] = sampled
    st.download_button("Download DB2 analysis (.xlsx)", data=lambda: convert_df_to_excel_bytes(export), icon=":material/download:",
                       file_name=f"db2_{job.key.split('|')[1]}_{src.schema}_analysis.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
