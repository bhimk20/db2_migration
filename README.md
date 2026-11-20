# Azure Migration Assistant

A single-user Streamlit app that inspects migration datasets (ARM ServiceNow, BA-to-Server mapping, RVTools, CMDB-CI-SERVER, Axonius), maps applications to servers, and recommends an Azure Database for PostgreSQL Flexible Server size. It also has a guided "Research & guide" page that teaches the sizing rules.

## Run it

**Team members (no Python needed):** download the zip for your OS from the Releases page (or the Actions run), unzip, and run `AzureMigrationAssistant` (`.exe` on Windows). A console window opens and so does your browser. Close the console window to stop.
- macOS: first launch may be blocked. Right-click the file, choose Open, then confirm.
- Windows: SmartScreen may warn about an unsigned app. Choose "More info", then "Run anyway".

**From source:**
```bash
./scripts/run.sh          # creates .venv on first run, then starts the app
```
Opens at `http://localhost:8501`. Press the **Sample data** button in the sidebar for synthetic data.

## Project layout

```
app.py               Streamlit entry point: page config, sidebar, navigation
app_pages/
  home.py            Upload, application lookup, sizing recommendation
  research.py        Guided lessons + full research reference
  db2_explorer.py    DB2 object inventory (tables, views, stored procedures)
  db2_assessment.py  DB2 sizing metrics, findings and Azure recommendation
core/
  azure_recommender.py  Sizing engine (SKU catalog, IOPS caps, rules)
  app_lookup.py         Application -> server lookup and Excel report
  dataset_loader.py     File loading and schema detection
  db2_metrics.py        Db2 collector (docker exec or ibm_db) and analyzer
  path_manager.py       Saved local paths (stored in ~/.azure_migration_assistant/)
  schemas.py            Dataset schema definitions
  sample_data.py        Synthetic sample data generator
docs/
  azure_research.md     Sizing research (source of truth for the rules, with a verification log)
  schema.md             Dataset column definitions
  archive/              Older research drafts
packaging/
  launcher.py           Entry point of the executable
  build.py              Builds the executable with PyInstaller
scripts/run.sh          Run from source
.github/workflows/build.yml   Builds Windows and macOS executables
.streamlit/config.toml  Theme
samples/                Local exports for your own use (git-ignored, never bundled)
```

## Build the executable

PyInstaller builds only for the OS it runs on.

```bash
pip install -r requirements.txt -r requirements-build.txt
python packaging/build.py        # -> dist/AzureMigrationAssistant-<os>.zip
```

For Windows and macOS together, use GitHub Actions: run **Build executables** from the Actions tab, or push a tag (`git tag v1.0.0 && git push --tags`) to attach both zips to a release.

The bundle contains the code, `docs/` and the theme only. Real exports in `samples/` are not included, and each user's saved file paths stay in their own home folder.

## DB2

Two pages share one connection. **DB2 explorer** lists the tables (keys, indexes), views and stored procedures/functions of a schema, with a rough rewrite effort per routine. **DB2 assessment** runs read-only queries (`MON_GET_*`, `ADMIN_GET_TAB_INFO`, `SYSCAT.*`) against a Db2 database, takes counter snapshots to derive peak CPU, IOPS and connections, lists migration risks (XML, VECTOR, MDC, routines) and feeds the sizing engine. For a ready-made test database, `scripts/setup_gsdb.sh [container] [database]` installs IBM's Great Outdoors sample (GS_DB; use schema GOSALES or GOSALESDW). Connect through a local Docker container (no driver needed) or over TCP with `pip install ibm_db`. The user needs SYSMON or DBADM. Short samples are a demo only: size production from a representative busy window.

## Azure sizing rules

Rules follow `docs/azure_research.md` (Canada Central, Flexible Server, v5 SKUs):

- **Production:** General Purpose (D-series, 4 GiB/vCore) by default. Memory Optimized (E-series) when the RAM target needs it or the source RAM:vCPU is above 4:1.
- **Non-production:** Burstable, dev/test only. No HA.
- **Advanced inputs:** peak CPU %, hot working set, data growth, peak IOPS and connection counts replace default estimates with your measurements.
- **Storage:** 12 months of growth +30% MVCC/WAL headroom, 32 GiB minimum. IOPS are the lower of the disk's and the VM's limit.


docker run -itd --name db2 --cap-add=IPC_LOCK --cap-add=IPC_OWNER --shm-size=1g -e LICENSE=accept -e DB2INST1_PASSWORD=db2inst1 -e DBNAME=SampleDB -p 50000:50000 icr.io/db2_community/db2