"""
Path Manager for Local Dataset File Persistence & Resolution.
Enables saving and resolving local file paths so datasets persist across app restarts
without storing large file byte buffers in memory.
"""

import json
import os
import re
import shutil
from pathlib import Path
from core.schemas import check_filename_includes, normalize_name

# Saved paths live in the user's home so they work for a packaged executable too (the bundle is read-only
# and shared, and must never ship one person's file paths).
APP_DIR = Path.home() / ".db2_migration"
CONFIG_DIR = APP_DIR
CONFIG_FILE = CONFIG_DIR / "saved_dataset_paths.json"
LEGACY_DIR = Path.home() / ".azure_migration_assistant"
SUPPORTED_EXTENSIONS = (".xlsx", ".xls", ".csv")


def _migrate_legacy_dir():
    """Copies the config folder from the previous app name once. The old folder is left in place."""
    try:
        if LEGACY_DIR.exists() and not APP_DIR.exists():
            shutil.copytree(LEGACY_DIR, APP_DIR)
    except Exception:
        pass


_migrate_legacy_dir()

# Where the user launched the app from (the packaged launcher changes the working directory).
LAUNCH_DIR = Path(os.environ.get("DB2M_LAUNCH_DIR") or os.environ.get("AMA_LAUNCH_DIR") or Path.cwd())
DEFAULT_SEARCH_DIRS = [LAUNCH_DIR / "samples", LAUNCH_DIR, Path.home() / "Downloads"]


def load_persistent_paths() -> dict:
    """Reads saved dataset paths from the user config file."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                valid = {}
                for k, v in data.items():
                    if isinstance(v, str) and Path(v).exists():
                        valid[k] = str(Path(v).resolve())
                return valid
        except Exception:
            return {}
    return {}


def save_persistent_path(slot_key: str, file_path: str):
    """Saves or updates a file path in the user config file."""
    paths = load_persistent_paths()
    if file_path and Path(file_path).exists():
        paths[slot_key] = str(Path(file_path).resolve())
    else:
        paths.pop(slot_key, None)
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(paths, f, indent=2)
    except Exception:
        pass


def clear_persistent_paths():
    """Removes all saved paths from the config file."""
    if CONFIG_FILE.exists():
        try:
            CONFIG_FILE.unlink()
        except Exception:
            pass


def find_local_system_path(filename_or_keyword: str, custom_dirs=None):
    """
    Checks whether a file exists on the local filesystem by exact path, filename,
    or keyword matching (lowercase, special characters removed).
    Returns resolved Path if found on disk, otherwise None.
    """
    if not filename_or_keyword:
        return None

    # 1. Exact path or relative path
    p = Path(filename_or_keyword).expanduser()
    if p.exists() and p.is_file():
        return p.resolve()

    target_clean = normalize_name(filename_or_keyword)
    dirs = (custom_dirs or []) + DEFAULT_SEARCH_DIRS

    # 2. Search candidate directories
    for d in dirs:
        dir_path = Path(d).expanduser().resolve()
        if not dir_path.is_dir():
            continue
        for f in dir_path.iterdir():
            if f.is_file() and f.suffix.lower() in [".xlsx", ".xls", ".csv"]:
                # Exact name match
                if f.name.lower() == str(filename_or_keyword).lower() or normalize_name(f.name) == target_clean:
                    return f.resolve()
                # Keyword containment match
                if target_clean in normalize_name(f.name):
                    return f.resolve()

    return None


def scan_default_local_files():
    """
    Scans ./samples and ./ for the 3 required dataset files.
    Returns dict mapping slot keys ('arm', 'ba', 'cmdb') to resolved Path or None.
    """
    found = {"arm": None, "ba": None, "cmdb": None}
    saved = load_persistent_paths()

    # Pre-populate from saved config if valid
    for slot in ["arm", "ba", "cmdb"]:
        if slot in saved and Path(saved[slot]).exists():
            found[slot] = Path(saved[slot]).resolve()

    # If any slot is missing, auto-discover from local directories
    if not found["arm"]:
        found["arm"] = find_local_system_path("ARM Service now")
    if not found["ba"]:
        found["ba"] = find_local_system_path("ba_as_server") or find_local_system_path("ba_as_a_server")
    if not found["cmdb"]:
        found["cmdb"] = find_local_system_path("cmdb_ci_server")

    return found


def clean_user_path(text: str) -> str:
    """Strips whitespace and surrounding quotes (Windows 'Copy as path' adds them), then expands env vars and '~'."""
    s = text.strip()
    while len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1].strip()
    s = os.path.expandvars(s)
    return os.path.expanduser(s)


def expand_user_paths(text: str) -> tuple[list[tuple[Path, bool]], list[str]]:
    """
    Turns pasted text (one path per line) into dataset files.
    Returns ([(path, from_folder), ...], [error messages]).
    A folder is scanned non-recursively; from_folder is True for files found that way.
    """
    files: list[tuple[Path, bool]] = []
    errors: list[str] = []
    seen: set[Path] = set()

    def add(path: Path, from_folder: bool):
        key = path.resolve()
        if key not in seen:
            seen.add(key)
            files.append((key, from_folder))

    tokens: list[str] = []
    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        whole = clean_user_path(raw_line)
        if Path(whole).exists() or ";" not in raw_line:
            tokens.append(whole)
        else:
            tokens.extend(clean_user_path(part) for part in raw_line.split(";") if part.strip())

    for token in tokens:
        if not token:
            continue
        p = Path(token)
        if not p.exists():
            errors.append(f"Not found: {token}")
        elif p.is_file():
            if p.suffix.lower() in SUPPORTED_EXTENSIONS:
                add(p, False)
            else:
                errors.append(f"Unsupported file type: {token}")
        elif p.is_dir():
            try:
                candidates = sorted(
                    (f for f in p.iterdir()
                     if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS and not f.name.startswith("~$")),
                    key=lambda f: f.name.lower(),
                )
            except OSError as e:
                errors.append(f"Cannot read folder: {token} ({e.strerror or e})")
                continue
            if not candidates:
                errors.append(f"No Excel or CSV files in: {token}")
            for f in candidates:
                add(f, True)

    return files, errors
