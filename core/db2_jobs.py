"""
Background Db2 assessment jobs.

One job per (connection, database, schema). It runs in a worker thread so the UI stays usable, and the
result stays in this process (survives reruns and page changes) and on disk (survives restarts).
No Streamlit calls in here: worker threads have no script context.
"""

import pickle
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from core.db2_metrics import Db2Source, analyze, collect_objects, collect_sampled, collect_static

CACHE_DIR = Path.home() / ".azure_migration_assistant" / "db2"
DEFAULT_INTERVAL_S = 30
DEFAULT_WINDOW_S = 60
SAMPLE_REUSE_S = 3600  # reuse a workload sample for another schema of the same database for this long

_JOBS: Dict[str, "Job"] = {}
_SAMPLES: Dict[str, tuple] = {}  # conn_key -> (taken_at, interval, window, DataFrame)
_LOCK = threading.Lock()


@dataclass
class Job:
    key: str
    status: str = "queued"      # queued | running | done | error
    stage: str = "Queued"
    progress: float = 0.0
    objects: Optional[Dict[str, Any]] = None   # available as soon as the catalog is read
    result: Optional[Dict[str, Any]] = None    # analyze() output, set when done
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


def job_key(src: Db2Source) -> str:
    return f"{conn_key(src)}|{src.schema.upper()}"


def _disk_path(key: str) -> Path:
    return CACHE_DIR / (re.sub(r"[^A-Za-z0-9_.-]+", "_", key) + ".pkl")


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


def _run(job: Job, src: Db2Source) -> None:
    try:
        job.status, job.started = "running", time.time()
        job.stage, job.progress = "Reading size and configuration", 0.05
        static = collect_static(src)

        job.stage, job.progress = "Reading tables, views and routines", 0.15
        objects = collect_objects(src)
        objects["schema"], objects["database"] = src.schema.upper(), src.database
        job.objects = objects

        sampled = None
        if job.sample_on:
            ck = conn_key(src)
            hit = _SAMPLES.get(ck)
            if hit and hit[1] == job.interval_s and hit[2] == job.window_s and time.time() - hit[0] < SAMPLE_REUSE_S:
                sampled = hit[3]
            else:
                job.stage, job.progress = f"Measuring workload for {job.window_s} s", 0.25

                def on_progress(frac: float) -> None:
                    job.progress = 0.25 + 0.65 * frac
                    job.stage = f"Measuring workload ({int(frac * 100)}%)"

                sampled = collect_sampled(src, static["host"]["cpus"], job.interval_s,
                                          duration_s=job.window_s, progress=on_progress)
                _SAMPLES[ck] = (time.time(), job.interval_s, job.window_s, sampled)

        job.stage, job.progress = "Analyzing", 0.95
        job.result = analyze(static, sampled, objects=objects)
        job.status, job.stage, job.progress, job.finished = "done", "Done", 1.0, time.time()
        _save(job)
    except Exception as exc:  # worker must never die silently
        job.status, job.error, job.finished = "error", str(exc) or type(exc).__name__, time.time()


def start_assessment(src: Db2Source, sample_on: bool = True, interval_s: int = DEFAULT_INTERVAL_S,
                     window_s: int = DEFAULT_WINDOW_S, force: bool = False) -> Job:
    """Start a background assessment unless one is running, or a finished one exists and `force` is off."""
    key = job_key(src)
    with _LOCK:
        job = _JOBS.get(key) or _load(key)
        if job and job.active:
            return job
        if job and job.status == "done" and not force:
            _JOBS[key] = job
            return job
        if force:
            _SAMPLES.pop(conn_key(src), None)
        job = Job(key=key, sample_on=sample_on, interval_s=int(interval_s), window_s=int(window_s))
        _JOBS[key] = job
    # worker gets its own copy: the password stays in memory only for the life of the run
    worker_src = Db2Source(**vars(src))
    threading.Thread(target=_run, args=(job, worker_src), daemon=True, name=f"db2-{key}").start()
    return job
