"""
Background Db2 assessment jobs.

One job per instance (a docker container, or host:port:user|database over TCP). It runs in a worker
thread so the UI stays usable. Every database of the instance is read in parallel (catalog first, then
workload sampling for all of them over the same window). The result stays in this process (survives
reruns and page changes) and on disk (survives restarts).
No Streamlit calls in here: worker threads have no script context.
"""

import pickle
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from core.db2_metrics import Db2Source, collect_objects, collect_sampled, collect_static
from core.path_manager import APP_DIR

CACHE_DIR = APP_DIR / "db2"
DEFAULT_INTERVAL_S = 30
DEFAULT_WINDOW_S = 60

_JOBS: Dict[str, "Job"] = {}
_LOCK = threading.Lock()


@dataclass
class Job:
    key: str
    status: str = "queued"      # queued | running | done | error
    stage: str = "Queued"
    progress: float = 0.0
    objects: Dict[str, dict] = field(default_factory=dict)  # database -> objects, filled as each catalog is read
    result: Optional[dict] = None  # {"host", "taken", "databases": {DB: {static, objects, sampled, error}}}
    error: str = ""
    started: float = 0.0
    finished: float = 0.0
    sample_on: bool = True
    interval_s: int = DEFAULT_INTERVAL_S
    window_s: int = DEFAULT_WINDOW_S

    @property
    def active(self) -> bool:
        return self.status in ("queued", "running")


def conn_key(src: Db2Source) -> str:
    """Identifies the database being assessed. The password is never part of it."""
    where = src.container or f"{src.host}:{src.port}:{src.user}"
    return f"{where}|{src.database}"


def instance_key(src: Db2Source) -> str:
    """Identifies the instance (container, or host:port:user|database over TCP). One job per instance."""
    if src.container:
        return src.container
    return f"{src.host}:{src.port}:{src.user}|{src.database}"


def job_key(src: Db2Source) -> str:
    return instance_key(src)


def _disk_path(key: str) -> Path:
    return CACHE_DIR / ("v2_" + re.sub(r"[^A-Za-z0-9_.-]+", "_", key) + ".pkl")


def _save(job: Job) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(_disk_path(job.key), "wb") as f:
            pickle.dump(job, f)
    except Exception:
        pass  # disk cache is a convenience


def _load(key: str) -> Optional[Job]:
    try:
        with open(_disk_path(key), "rb") as f:
            job = pickle.load(f)
        return job if isinstance(job, Job) and job.status == "done" else None
    except Exception:
        return None


def get_job(src: Db2Source) -> Optional[Job]:
    key = job_key(src)
    with _LOCK:
        job = _JOBS.get(key)
        if job is None:
            job = _load(key)
            if job:
                _JOBS[key] = job
        return job


def _error_text(exc: Exception) -> str:
    return (str(exc) or type(exc).__name__)[:300]


def _run(job: Job, src: Db2Source, databases: List[str]) -> None:
    """Phase 1 reads catalogs of all databases, phase 2 samples workload, phase 3 assembles the result.

    A database that fails is marked with its error and the others continue. The job fails only when
    no database could be read at all.
    """
    try:
        job.status, job.started = "running", time.time()
        n = len(databases)
        if n == 0:
            raise RuntimeError("No database to read")
        reads: Dict[str, dict] = {
            db: {"static": None, "objects": None, "sampled": None, "error": ""} for db in databases
        }

        # phase 1: catalog of every database, at most 4 at once
        progress_lock = threading.Lock()
        done = 0
        job.stage, job.progress = f"Reading {n} databases (0/{n} done)", 0.05

        def read(db: str) -> None:
            nonlocal done
            entry = reads[db]
            try:
                dsrc = Db2Source(**{**vars(src), "database": db, "schema": ""})
                static = collect_static(dsrc)
                objects = collect_objects(dsrc)
                objects["database"] = db
                entry["static"], entry["objects"] = static, objects
                job.objects[db] = objects
            except Exception as exc:
                entry["static"] = entry["objects"] = None
                entry["error"] = _error_text(exc)
            with progress_lock:
                done += 1
                job.stage = f"Reading {n} databases ({done}/{n} done)"
                job.progress = 0.05 + 0.25 * done / n

        with ThreadPoolExecutor(max_workers=min(4, n)) as pool:
            list(pool.map(read, databases))

        ok = [db for db in databases if reads[db]["static"] is not None]
        if not ok:
            first_error = next((reads[db]["error"] for db in databases if reads[db]["error"]), "no data")
            raise RuntimeError(first_error)

        # phase 2: workload of every readable database, in parallel, same window
        if job.sample_on:
            fractions = {db: 0.0 for db in ok}
            job.stage, job.progress = "Measuring workload (0%)", 0.30

            def sample(db: str) -> None:
                entry = reads[db]

                def on_progress(frac: float) -> None:
                    with progress_lock:
                        fractions[db] = frac
                        mean = sum(fractions.values()) / len(fractions)
                        job.progress = 0.30 + 0.65 * mean
                        job.stage = f"Measuring workload ({int(mean * 100)}%)"

                try:
                    dsrc = Db2Source(**{**vars(src), "database": db, "schema": ""})
                    entry["sampled"] = collect_sampled(dsrc, entry["static"]["host"]["cpus"], job.interval_s,
                                                       duration_s=job.window_s, progress=on_progress)
                except Exception:
                    entry["sampled"] = None  # sampling failure does not fail the job

            with ThreadPoolExecutor(max_workers=len(ok)) as pool:
                list(pool.map(sample, ok))

        # phase 3: assemble the result
        job.result = {
            "host": reads[ok[0]]["static"]["host"],
            "taken": time.time(),
            "databases": reads,
        }
        job.status, job.stage, job.progress, job.finished = "done", "Done", 1.0, time.time()
        _save(job)
    except Exception as exc:  # worker must never die silently
        job.status, job.error, job.finished = "error", str(exc) or type(exc).__name__, time.time()


def start_assessment(src: Db2Source, databases: Optional[List[str]] = None, sample_on: bool = True,
                     interval_s: int = DEFAULT_INTERVAL_S, window_s: int = DEFAULT_WINDOW_S,
                     force: bool = False) -> Job:
    """Start a background assessment of the instance unless one is running, or a finished one exists and `force` is off."""
    key = job_key(src)
    dbs = list(databases) if databases else [src.database]
    with _LOCK:
        job = _JOBS.get(key) or _load(key)
        if job and job.active:
            return job
        if job and job.status == "done" and not force:
            _JOBS[key] = job
            return job
        job = Job(key=key, sample_on=sample_on, interval_s=int(interval_s), window_s=int(window_s))
        _JOBS[key] = job
    # worker gets its own copy: the password stays in memory only for the life of the run
    worker_src = Db2Source(**vars(src))
    threading.Thread(target=_run, args=(job, worker_src, dbs), daemon=True, name=f"db2-{key}").start()
    return job
