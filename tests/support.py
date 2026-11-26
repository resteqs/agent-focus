"""Point every Agent Focus data path at a throwaway sandbox before `server` is imported.

Import this module first in every test file.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SANDBOX = Path(tempfile.mkdtemp(prefix="agent-focus-tests-"))
atexit.register(shutil.rmtree, SANDBOX, ignore_errors=True)

DATA_DIR = SANDBOX / "data"
DATA_DIR.mkdir()
os.environ["AGENT_FOCUS_DATA_DIR"] = str(DATA_DIR)
os.environ["ANKI_HEADLESS_HOME"] = str(SANDBOX / "anki-headless")
os.environ.pop("ANKI_SYNC_ENDPOINT", None)


def write_config(data_dir: Path, **overrides: object) -> None:
    config = {
        "anki_headless_home": str(SANDBOX / "anki-headless"),
        "obsidian_note_path": "",
        **overrides,
    }
    (data_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")


write_config(DATA_DIR)
