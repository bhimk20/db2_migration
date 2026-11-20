"""
DB2 assessment page: collect sizing metrics from a Db2 database, analyze them and feed the Azure recommender.
"""

import pandas as pd
import streamlit as st

from core.azure_recommender import recommend_azure_postgresql
from core.dataset_loader import convert_df_to_excel_bytes
from app_pages.db2_connection import connection_form
from core.db2_jobs import get_job

def _size(gb: float) -> str:
    return f"{gb * 1024:,.1f} MB" if gb < 1 else f"{gb:,.1f} GB" if gb < 1024 else f"{gb / 1024:,.2f} TB"


st.title("DB2 assessment")
st.caption("Read-only queries against a Db2 database. Results feed the Azure PostgreSQL sizing engine. "
           "Nothing is changed on the database.")
with st.expander("New here? How this page works"):
    st.markdown(
        "1. **Connect** below (a Docker container, or over the network), then pick the database and schema from the dropdowns.\n"
        "2. **It assesses in the background** as soon as you pick a schema, and keeps the result. The page reads facts that are stored in the database (sizes, tables, settings) "
        "and then *watches* it for a while to measure how busy it is.\n"
        "3. **Read the results:** *Summary* is the size and load, *Findings* is what could be hard to migrate, "
        "and the last section is the Azure PostgreSQL size we suggest.\n\n"
        "Why watch for a while? Db2 only keeps running totals (like a car's odometer), not history. To get a "
        "rate such as *operations per second*, the page reads the totals, waits, reads them again and divides.")

src = connection_form()
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
    st.stop()

job = get_job(src)
res = job.result if job else None
if not res:
    if not (job and job.status == "error"):
        st.info("The assessment is running in the background. Results appear here when it finishes.", icon=":material/hourglass_top:")
    st.stop()

sm = res["summary"]
st.subheader("Summary")
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
    x = st.columns(4)
    x[0].metric("Sampled window", f"{sm['window_s']:.0f} s", f"{sm['samples']} samples", delta_color="off", delta_arrow="off",
                help="How long the page actually measured. The longer and the busier the period, the more you can trust the peaks.")
    x[1].metric("Data hit ratio", f"{sm['data_hit_pct']:.1f} %" if "data_hit_pct" in sm else "n/a",
                help="Share of data reads served from memory instead of disk. Near 100% = memory is big enough. "
                     "Low (under ~90%) can mean more memory is needed, or the cache was still cold.")
    x[2].metric("Index hit ratio", f"{sm['index_hit_pct']:.1f} %" if "index_hit_pct" in sm else "n/a",
                help="Same as the data hit ratio, but for indexes (lookup structures).")
    cap = res["sampled"].attrs.get("cpu_limit") if res["sampled"] is not None else None
    x[3].metric("CPU % basis", f"{cap:g} cores (cap)" if cap else f"{sm['host_cpus']:.0f} cores (host)",
                help="CPU % is measured against this many cores. If the container is capped, the cap is used; otherwise all host cores.")

st.subheader("Findings")
st.caption("Things that affect the migration or how far to trust the numbers. "
           ":material/error: **Risk** = needs rework before or during migration. "
           ":material/warning: **Warning** = may make the sizing unreliable. "
           ":material/info: **Info** = good to know, usually low effort.")
icons = {"risk": ":material/error:", "warning": ":material/warning:", "info": ":material/info:"}
for f in sorted(res["findings"], key=lambda x: ["risk", "warning", "info"].index(x["level"])):
    with st.container(border=True):
        st.markdown(f"{icons[f['level']]} **{f['title']}**")
        st.caption(f["detail"])

t_tab, t_ts, t_samp = st.tabs(["Tables", "Tablespaces", "Samples"])
with t_tab:
    st.caption("Size per table in KB. 'rows' = -1 means Db2 has no statistics for it (RUNSTATS never ran).")
    st.dataframe(res["tables"], hide_index=True)
with t_ts:
    st.caption("Tablespaces are the disk areas Db2 stores tables in. Allocated = reserved, used = filled.")
    st.dataframe(res["tablespaces"], hide_index=True)
with t_samp:
    if res["sampled"] is not None and len(res["sampled"]):
        st.caption("One row per reading interval: the rates measured between two consecutive readings.")
        st.dataframe(res["sampled"], hide_index=True)
    else:
        st.caption("No usable samples.")

st.subheader("Azure PostgreSQL recommendation")
st.caption("A starting point for the Azure PostgreSQL Flexible Server size, from the measured CPU, memory, disk and IOPS. "
           "Switch on *Production workload* to get a high-availability (zone-redundant) setup.")
is_prod = st.toggle("Production workload", value=False)
kw = dict(res["recommender_kwargs"])
rec = recommend_azure_postgresql(is_production=is_prod, **kw)
sku = rec["recommended_sku"]
with st.container(border=True):
    st.markdown(f"#### `{sku['sku']}`")
    r = st.columns(4)
    r[0].metric("Tier", sku["tier"])
    r[1].metric("vCores", sku["vcpus"])
    r[2].metric("Memory", f"{sku['memory_gib']} GiB")
    r[3].metric("Storage", f"{rec['target_storage_gib']:,} GiB")
    st.caption(f"HA: {'zone-redundant' if rec['ha_enabled'] else 'standalone'}")
with st.expander("Inputs passed to the recommender"):
    st.json(kw)
if sm.get("samples", 0) < 10:
    st.warning(f"Only {sm.get('samples', 0)} sample(s) were taken. Sizing for production needs metrics over a "
               "representative busy period.", icon=":material/schedule:")

export = {
    "Summary": pd.DataFrame([{"Metric": k, "Value": v} for k, v in sm.items()]),
    "Findings": pd.DataFrame(res["findings"]),
    "Tables": res["tables"],
    "Tablespaces": res["tablespaces"],
    "Recommender inputs": pd.DataFrame([{"Input": k, "Value": v} for k, v in kw.items()]),
}
if res["sampled"] is not None and len(res["sampled"]):
    export["Samples"] = res["sampled"]
st.download_button("Download DB2 analysis (.xlsx)", data=lambda: convert_df_to_excel_bytes(export), icon=":material/download:",
                   file_name=f"db2_{job.key.split('|')[1]}_{src.schema}_analysis.xlsx",
                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
