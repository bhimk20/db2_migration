"""
Shared Db2 connection panel used by the DB2 explorer and DB2 assessment pages (not a page itself).

Flow: pick a container (or enter TCP details) -> Connect -> pick database and schema from dropdowns
filled from the server -> a background assessment starts on its own and its result is kept, so moving
between pages or coming back later does not repeat any work.
"""

import time
from typing import Optional

import streamlit as st

from core.db2_jobs import DEFAULT_INTERVAL_S, DEFAULT_WINDOW_S, Job, conn_key, get_job, start_assessment
from core.db2_metrics import Db2Error, Db2Source, list_containers, list_databases, list_schemas

_KEEP = dict(persist_state="session")  # widget values survive page changes


@st.cache_data(ttl=15, show_spinner=False)
def _containers() -> list:
    return list_containers()


@st.cache_data(ttl=300, show_spinner=False)
def _schemas(key: str, _src: Db2Source) -> list:
    return list_schemas(_src)


def _sample_settings() -> dict:
    s = st.session_state
    return dict(sample_on=s.get("db2_sample_on", True), interval_s=int(s.get("db2_interval", DEFAULT_INTERVAL_S)),
                window_s=int(s.get("db2_window", DEFAULT_WINDOW_S)))


@st.fragment(run_every=2)
def _live_progress(src: Db2Source, needs: str, had_objects: bool) -> None:
    """Polls the background job. Reruns the whole page once when what the page needs becomes available."""
    job = get_job(src)
    if job is None:
        return
    if not job.active or (needs == "objects" and job.objects is not None and not had_objects):
        st.rerun()
    st.progress(job.progress, text=f"Assessing in the background: {job.stage}. You can keep browsing.")


def _status(src: Db2Source, job: Job, needs: str) -> None:
    if job.active:
        _live_progress(src, needs, job.objects is not None)
        return
    if job.status == "error":
        st.error(f"Assessment failed: {job.error}", icon=":material/error:")
    else:
        st.caption(f":material/check_circle: Assessment saved {time.strftime('%H:%M', time.localtime(job.finished))}"
                   f" ({'workload sampled for ' + str(job.window_s) + ' s' if job.sample_on else 'no workload sampling'}).")
    if st.button("Re-run assessment" if job.status == "done" else "Retry", icon=":material/refresh:", key="db2_rerun"):
        start_assessment(src, force=True, **_sample_settings())
        st.rerun()


def connection_form(needs: str = "result") -> Optional[Db2Source]:
    """Render the panel. Returns the chosen source once connected and a schema is picked, else None.

    `needs` is "objects" for pages that only need the catalog (shown earlier) or "result" for the full analysis.
    """
    with st.container(border=True):
        mode = st.segmented_control("Connection type", ["Docker container", "TCP (ibm_db)"], default="Docker container",
                                    required=True, key="db2_mode", **_KEEP)
        docker_mode = mode == "Docker container"
        container = host = user = pw = ""
        port = 50000
        row = st.container(horizontal=True, vertical_alignment="bottom")
        if docker_mode:
            found = _containers()
            if found:
                container = row.selectbox("Container", found, accept_new_options=True, key="db2_container_pick",
                                          help="Running Db2 containers found via docker. Type a name to use another.", **_KEEP) or ""
            else:
                container = row.text_input("Container", key="db2_container_text", placeholder="db2", **_KEEP,
                                           help="No running Db2 container detected. Start it, or type its name.")
            ident = container
        else:
            host = row.text_input("Host", key="db2_host", **_KEEP)
            port = int(row.number_input("Port", value=50000, step=1, key="db2_port", **_KEEP))
            user = row.text_input("User", key="db2_user", **_KEEP)
            pw = row.text_input("Password", type="password", key="db2_pw", **_KEEP)
            ident = f"{host}:{port}:{user}"
        fp = (mode, ident)
        connect = row.button("Connect", type="primary", icon=":material/power:", disabled=not (container or host))

        if connect:
            try:
                with st.spinner("Connecting"):
                    dbs = list_databases(Db2Source(container=container, host=host, port=port, user=user, password=pw))
                st.session_state["db2_conn"] = {"fp": fp, "databases": dbs}
                _containers.clear()
            except (Db2Error, ValueError, OSError) as exc:
                st.session_state.pop("db2_conn", None)
                st.error(f"Could not connect: {exc}", icon=":material/error:")
                return None

        conn = st.session_state.get("db2_conn")
        if not conn or conn["fp"] != fp:
            st.caption("Choose a connection and press **Connect**. Databases and schemas are then offered in dropdowns.")
            return None

        pick = st.container(horizontal=True, vertical_alignment="bottom")
        dbs = conn["databases"]
        if dbs:
            if st.session_state.get("db2_database_pick") not in dbs:
                st.session_state.pop("db2_database_pick", None)
            database = pick.selectbox("Database", dbs, key="db2_database_pick", **_KEEP)
        else:
            database = pick.text_input("Database", key="db2_database_text", **_KEEP,
                                       help="Db2 cannot list databases over TCP. Type the database name.")
        if not database:
            st.caption("Enter the database name to list its schemas.")
            return None

        src = Db2Source(database=database, container=container, host=host, port=port, user=user, password=pw)
        try:
            schemas = _schemas(conn_key(src), src)
        except (Db2Error, ValueError, OSError) as exc:
            st.error(f"Could not list schemas of {database}: {exc}", icon=":material/error:")
            return None
        if not schemas:
            st.warning(f"{database} has no user schemas with objects.", icon=":material/folder_off:")
            return None
        if st.session_state.get("db2_schema_pick") not in schemas:
            st.session_state.pop("db2_schema_pick", None)
        src.schema = pick.selectbox("Schema", schemas, key="db2_schema_pick", **_KEEP,
                                    help=f"{len(schemas)} schemas in {database}. One schema is assessed at a time.")

        job = get_job(src) or start_assessment(src, **_sample_settings())
        _status(src, job, needs)
    return src
