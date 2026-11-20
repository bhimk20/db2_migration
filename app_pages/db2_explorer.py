"""
DB2 explorer page: browse what a Db2 database contains (tables, views, stored procedures / functions),
for all its schemas or one picked schema.
Inventory only. The catalog is read by the background assessment that starts when you connect,
so it is already there when you come back. Sizing and the Azure recommendation live on the DB2 assessment page.
"""

import pandas as pd
import streamlit as st

from app_pages.db2_connection import connection_form
from core.dataset_loader import convert_df_to_excel_bytes
from core.db2_jobs import get_job
from core.db2_scope import scope_objects

st.title("DB2 explorer")
st.caption("Read-only look at the objects in one database, or one schema of it: tables, views, stored procedures "
           "and functions.")

with st.expander("How to read this page"):
    st.markdown(
        "- **Tables**: structure per table. 'rows' = -1 means Db2 has no statistics (RUNSTATS never ran).\n"
        "- **Views**: views and how many tables / views each one depends on. Invalid = depends on a changed or "
        "dropped object.\n"
        "- **Stored procedures & functions**: routines written in Db2 SQL PL must be rewritten for PostgreSQL. "
        "Effort is a rough guide: High = external language, two or more Db2-only features, or very long.\n"
        "- Nothing is changed on the database."
    )

src = connection_form(needs="objects")
if src is None:
    st.info("Connect to a database in the sidebar.", icon=":material/arrow_back:")
    st.stop()
job = get_job(src)
objs = scope_objects(job.objects, src.database, src.schema) if job else None
if not objs:
    if not (job and job.status == "error"):
        st.info("Reading the catalog. The tables appear here in a few seconds.", icon=":material/hourglass_top:")
    st.stop()

tdf, vdf, rdf = objs["tables"], objs["views"], objs["routines"]
scope_name = objs["schema"] or "all schemas"
st.subheader(f"{objs['database']} / {scope_name}")
m = st.columns(5)
m[0].metric("Tables", len(tdf))
m[1].metric("Views", len(vdf))
m[2].metric("Stored procedures", int((rdf["type"] == "Procedure").sum()))
m[3].metric("Functions", int((rdf["type"] == "Function").sum()))
m[4].metric("Triggers", objs["counts"]["triggers"])

t_tab, t_view, t_rt = st.tabs(["Tables", "Views", "Stored procedures & functions"])
with t_tab:
    with st.expander("Column guide"):
        st.caption("Structure per table. 'rows' = -1 means Db2 has no statistics (RUNSTATS never ran).")
    st.markdown("**Largest tables by rows**")
    with_stats = tdf[tdf["rows"] >= 0]
    if len(with_stats):
        top = with_stats.nlargest(15, "rows").copy()
        multi_schema = tdf["schema"].nunique() > 1
        top["label"] = (top["schema"] + "." + top["table"]) if multi_schema else top["table"]
        st.bar_chart(top, x="label", y="rows", horizontal=True, sort=False,
                     x_label="Table", y_label="Rows", alt="Bar chart of the 15 largest tables by row count")
    else:
        st.caption("No table statistics yet.")
    n_stale = int((tdf["rows"] < 0).sum())
    if n_stale:
        st.caption(f"{n_stale} table(s) have no statistics and are left out of the chart.")
    q = st.text_input("Filter tables", key="db2_filter_tables", placeholder="name contains...")
    st.dataframe(tdf[tdf["table"].str.contains(q, case=False, regex=False)] if q else tdf, hide_index=True,
                 column_config={"rows": st.column_config.NumberColumn("Rows", format="localized")},
                 alt="Table inventory with schema, columns, rows and keys per table")
with t_view:
    with st.expander("Column guide"):
        st.caption("Views and how many tables / views each one depends on. Invalid = depends on a changed or dropped object.")
    st.markdown("**Views with most dependencies**")
    if len(vdf):
        top_views = vdf.nlargest(15, "dependencies").copy()
        multi_schema = vdf["schema"].nunique() > 1
        top_views["label"] = (top_views["schema"] + "." + top_views["view"]) if multi_schema else top_views["view"]
        st.bar_chart(top_views, x="label", y="dependencies", horizontal=True, sort=False,
                     x_label="View", y_label="Dependencies",
                     alt="Bar chart of the 15 views with the most dependencies")
        is_valid = vdf["valid"].str.upper() == "Y"
        is_invalid = vdf["valid"].str.upper() == "N"
        st.caption(f"{int(is_valid.sum())} valid, {int(is_invalid.sum())} invalid view(s).")
    else:
        st.caption("No views in this selection.")
    view_filter = st.segmented_control("Show", ["All", "Invalid only"], default="All", key="db2_view_filter")
    vshow = vdf[vdf["valid"].str.upper() == "N"] if view_filter == "Invalid only" else vdf
    st.dataframe(vshow, hide_index=True, alt="View inventory with schema, validity and dependencies")
with t_rt:
    with st.expander("Column guide"):
        st.caption("Routines written in Db2 SQL PL must be rewritten for PostgreSQL. Columns after 'text_length' are "
                   "Db2-specific features found in the routine (1 = used). Effort is a rough guide: High = external "
                   "language, two or more Db2-only features, or very long.")
    if len(rdf):
        order = ["Low", "Medium", "High"]
        edf = (rdf["effort"].value_counts().reindex(order, fill_value=0)
               .rename("routines").rename_axis("effort").reset_index())
        flags = {"cursor": "Cursor", "handler": "Handler", "dynamic_sql": "Dynamic SQL", "signal": "Signal",
                 "diagnostics": "Get diagnostics", "temp_table": "Temporary table"}
        fdf = pd.DataFrame({"feature": list(flags.values()),
                            "routines": [int(rdf[c].sum()) for c in flags]})

        c_effort, c_feature = st.columns(2)
        with c_effort:
            with st.container(border=True):
                st.markdown("**Routine rewrite effort**")
                st.bar_chart(edf, x="effort", y="routines", x_label="Effort", y_label="Routines",
                             alt="Bar chart of stored procedures and functions by rewrite effort (low, medium, high)")
        with c_feature:
            with st.container(border=True):
                st.markdown("**Db2-specific features**")
                st.bar_chart(fdf, x="feature", y="routines", horizontal=True, sort=False,
                             x_label="Feature", y_label="Routines",
                             alt="Bar chart of routines using each Db2-specific feature")

        effort_sel = st.pills("Effort", order, selection_mode="multi",
                              default=order, key="db2_routine_effort")
        rshow = rdf[rdf["effort"].isin(effort_sel or [])]
        st.dataframe(rshow, hide_index=True,
                     column_config={"text_length": st.column_config.NumberColumn("Text length", format="localized")},
                     alt="Stored procedures and functions with schema, type, language and Db2-specific feature flags")
    else:
        st.caption("No stored procedures or functions in this schema.")

st.download_button("Download object inventory (.xlsx)",
                   data=lambda: convert_df_to_excel_bytes({"Tables": tdf, "Views": vdf, "Routines": rdf}),
                   icon=":material/download:",
                   file_name=f"db2_{objs['database']}_{objs['schema'] or 'all'}_objects.xlsx",
                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
