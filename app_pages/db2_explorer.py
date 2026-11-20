"""
DB2 explorer page: browse what a Db2 schema contains (tables, views, stored procedures / functions).
Inventory only. The catalog is read by the background assessment that starts when you pick a schema,
so it is already there when you come back. Sizing and the Azure recommendation live on the DB2 assessment page.
"""

import streamlit as st

from app_pages.db2_connection import connection_form
from core.dataset_loader import convert_df_to_excel_bytes
from core.db2_jobs import get_job

st.title("DB2 explorer")
st.caption("Read-only look at the objects in one schema: tables, views, stored procedures and functions. "
           "Nothing is changed on the database.")

src = connection_form(needs="objects")
if src is None:
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

t_tab, t_view, t_rt = st.tabs(["Tables", "Views", "Stored procedures & functions"])
with t_tab:
    st.caption("Structure per table. 'rows' = -1 means Db2 has no statistics (RUNSTATS never ran).")
    q = st.text_input("Filter tables", key="db2_filter_tables", placeholder="name contains...")
    st.dataframe(tdf[tdf["table"].str.contains(q, case=False, regex=False)] if q else tdf, hide_index=True)
with t_view:
    st.caption("Views and how many tables / views each one depends on. Invalid = depends on a changed or dropped object.")
    st.dataframe(vdf, hide_index=True)
with t_rt:
    st.caption("Routines written in Db2 SQL PL must be rewritten for PostgreSQL. Columns after 'text_length' are Db2-specific "
               "features found in the routine (1 = used). Effort is a rough guide: High = external language, "
               "two or more Db2-only features, or very long.")
    if len(rdf):
        st.dataframe(rdf, hide_index=True)
    else:
        st.caption("No stored procedures or functions in this schema.")

st.download_button("Download object inventory (.xlsx)",
                   data=lambda: convert_df_to_excel_bytes({"Tables": tdf, "Views": vdf, "Routines": rdf}),
                   icon=":material/download:", file_name=f"db2_{objs.get('database', 'db')}_{objs.get('schema', 'schema')}_objects.xlsx",
                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
