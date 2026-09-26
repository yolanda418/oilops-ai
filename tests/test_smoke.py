"""Headless smoke test for STEP 1 + STEP 2."""

from __future__ import annotations

import os
import re
import sys
import tempfile
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_PATH = PROJECT_ROOT / "app.py"
STREAMLIT_IMPORT_RE = re.compile(r"^import streamlit as st.*$", re.MULTILINE)


def _load_app_module_with_stub():
    src = APP_PATH.read_text(encoding="utf-8")

    # Stub block: mimics the parts of streamlit that app.py uses.
    # session_state is a real dict so __setitem__/__getitem__/__delitem__
    # work; everything else is a no-op _Flex. Buttons must return False
    # by default so we don't accidentally trigger code paths like
    # `if st.button(...): st.rerun()` during import-time smoke runs.
    # date_input returns a real datetime.date so STEP 9 module-level
    # `compute_weekly_facts(conn, _weekly_ref_date)` does not raise during
    # smoke runs (weekly_summary validates isinstance(..., date)).
    stub_block = (
        "class _Flex:\n"
        "    def __init__(self, name=None): self._name = name\n"
        "    def __call__(self, *a, **k):\n"
        "        if self._name == 'button': return False\n"
        "        if self._name == 'date_input': return date.today()\n"
        "        return _Flex()\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *e): return False\n"
        "    def __iter__(self): return iter([])\n"
        "    def __bool__(self): return False\n"
        "    def __len__(self): return 0\n"
        "    def __getattr__(self, n):\n"
        "        if n in ('button', 'file_uploader', 'date_input'): return _Flex(n)\n"
        "        return _Flex()\n"
        "class _SessionState(dict):\n"
        "    def __getattr__(self, k):\n"
        "        if k in self: return self[k]\n"
        "        return None\n"
        "    def __setattr__(self, k, v): self[k] = v\n"
        "    def __delattr__(self, k):\n"
        "        if k in self: del self[k]\n"
        "class _StreamlitStub:\n"
        "    def __init__(self):\n"
        "        self.session_state = _SessionState()\n"
        "    def rerun(self): raise SystemExit(0)\n"
        "    def __getattr__(self, n):\n"
        "        if n == 'session_state': return self.session_state\n"
        "        if n in ('button', 'file_uploader', 'date_input'): return _Flex(n)\n"
        "        return _Flex()\n"
        "import sys as _sys\n"
        "from datetime import date as _date_cls\n"
        "_streamlit_stub = _StreamlitStub()\n"
        "_sys.modules['streamlit'] = _streamlit_stub\n"
        "st = _streamlit_stub\n"
        "date = _date_cls\n"
    )

    patched, n = STREAMLIT_IMPORT_RE.subn(
        stub_block + "# stubbed\n", src
    )
    assert n == 1, f"Expected exactly 1 streamlit import to stub, found {n}"

    namespace = {"__name__": "_app_smoke_", "__file__": str(APP_PATH)}
    exec(compile(patched, str(APP_PATH), "exec"), namespace)
    return namespace


def test_app_module_loads_with_streamlit_stubbed():
    ns = _load_app_module_with_stub()
    assert ns is not None


def test_app_module_exposes_db_helpers():
    ns = _load_app_module_with_stub()
    for sym in ("init_db", "get_connection", "list_tables", "list_indexes"):
        assert sym in ns, f"app.py must expose {sym}"


def test_app_creates_db_file_on_import():
    tmpdir = tempfile.mkdtemp(prefix="oilops_smoke_")
    os.environ["OILOPS_DB_PATH"] = os.path.join(tmpdir, "smoke.db")
    for mod in [m for m in list(sys.modules)
                if m == "app" or m.startswith("database.")]:
        del sys.modules[mod]
    _load_app_module_with_stub()
    assert Path(tmpdir, "smoke.db").exists(), (
        "STEP 1 expectation: importing app.py triggers init_db() which "
        "creates the SQLite file. The file should exist after import."
    )


def test_streamlit_is_eventually_installable():
    result = subprocess.run(
        [sys.executable, "-c", "import streamlit; assert streamlit.__version__"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

