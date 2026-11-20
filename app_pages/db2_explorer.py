"""
DB2 explorer page: browse what a Db2 schema contains (tables, views, stored procedures / functions).
Inventory only. The catalog is read by the background assessment that starts when you pick a schema,
so it is already there when you come back. Sizing and the Azure recommendation live on the DB2 assessment page.
"""

import pandas as pd
import streamlit as st

from app_pages.db2_connection import connection_form
from core.dataset_loader import convert_df_to_excel_bytes
from core.db2_jobs import get_job

st.title("DB2 explorer")
st.caption("Read-only look at the objects in one schema: tables, views, stored procedures and functions.")

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
objs = job.objects if job else None
if not objs:
    if not (job and job.status == "error"):
        st.info("Reading the catalog. The tables appear here in a few seconds.", icon=":material/hourglass_top:")
    st.stop()

tdf, vdf, rdf = objs["tables"], objs["views"], objs["routines"]
st.subheader(f"{objs.get('database', '')} / {objs.get('schema', '')}")
m = st.columns(5)
m[0].metric("Tables", len(tdf))
m[1].metric("Views", len(vdf))
m[2].metric("Stored procedures", int((rdf["type"] == "Procedure").sum()))
m[3].metric("Functions", int((rdf["type"] == "Function").sum()))
m[4].metric("Triggers", objs["counts"]["triggers"])

# Charts
c_left, c_right = st.columns(2)
with c_left:
    with st.container(border=True):
        st.markdown("**Largest tables by rows**")
        with_stats = tdf[tdf["rows"] >= 0]
        if len(with_stats):
            top = with_stats.nlargest(15, "rows")[["table", "rows"]]
            st.bar_chart(top, x="table", y="rows", horizontal=True, sort=False,
                         x_label="Table", y_label="Rows", alt="Bar chart of the 15 largest tables by row count")
        else:
            st.caption("No table statistics yet.")
        n_stale = int((tdf["rows"] < 0).sum())
        if n_stale:
            st.caption(f"{n_stale} table(s) have no statistics and are left out.")
with c_right:
    with st.container(border=True):
        st.markdown("**Routine rewrite effort**")
        if len(rdf):
            order = ["Low", "Medium", "High"]
            edf = (rdf["effort"].value_counts().reindex(order, fill_value=0)
                   .rename("routines").rename_axis("effort").reset_index())
            st.bar_chart(edf, x="effort", y="routines", x_label="Effort", y_label="Routines",
                         alt="Bar chart of stored procedures and functions by rewrite effort (low, medium, high)")
        else:
            st.caption("No stored procedures or functions in this schema.")

t_tab, t_view, t_rt = st.tabs(["Tables", "Views", "Stored procedures & functions"])
with t_tab:
    with st.expander("Column guide"):
        st.caption("Structure per table. 'rows' = -1 means Db2 has no statistics (RUNSTATS never ran).")
    q = st.text_input("Filter tables", key="db2_filter_tables", placeholder="name contains...")
    st.dataframe(tdf[tdf["table"].str.contains(q, case=False, regex=False)] if q else tdf, hide_index=True,
                 column_config={"rows": st.column_config.NumberColumn("Rows", format="localized")},
                 alt="Table inventory with columns, rows and keys per table")
with t_view:
    with st.expander("Column guide"):
        st.caption("Views and how many tables / views each one depends on. Invalid = depends on a changed or dropped object.")
    view_filter = st.segmented_control("Show", ["All", "Invalid only"], default="All", key="db2_view_filter")
    vshow = vdf[vdf["valid"].str.upper() == "N"] if view_filter == "Invalid only" else vdf
    st.dataframe(vshow, hide_index=True, alt="View inventory with validity and dependencies")
with t_rt:
    with st.expander("Column guide"):
        st.caption("Routines written in Db2 SQL PL must be rewritten for PostgreSQL. Columns after 'text_length' are "
                   "Db2-specific features found in the routine (1 = used). Effort is a rough guide: High = external "
                   "language, two or more Db2-only features, or very long.")
    if len(rdf):
        effort_sel = st.pills("Effort", ["Low", "Medium", "High"], selection_mode="multi",
                              default=["Low", "Medium", "High"], key="db2_routine_effort")
        rshow = rdf[rdf["effort"].isin(effort_sel or [])]
        st.dataframe(rshow, hide_index=True,
                     column_config={"text_length": st.column_config.NumberColumn("Text length", format="localized")},
                     alt="Stored procedures and functions with type, language and Db2-specific feature flags")

        flags = {"cursor": "Cursor", "handler": "Handler", "dynamic_sql": "Dynamic SQL", "signal": "Signal",
                 "diagnostics": "Get diagnostics", "temp_table": "Temporary table"}
        with st.expander("Db2-specific features found"):
            fdf = pd.DataFrame({"feature": list(flags.values()),
                                "routines": [int(rdf[c].sum()) for c in flags]})
            st.bar_chart(fdf, x="feature", y="routines", horizontal=True, sort=False,
                         x_label="Feature", y_label="Routines",
                         alt="Bar chart of routines using each Db2-specific feature")
    else:
        st.caption("No stored procedures or functions in this schema.")

st.download_button("Download object inventory (.xlsx)",
                   data=lambda: convert_df_to_excel_bytes({"Tables": tdf, "Views": vdf, "Routines": rdf}),
                   icon=":material/download:", file_name=f"db2_{objs.get('database', 'db')}_{objs.get('schema', 'schema')}_objects.xlsx",
                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
