"""Public-demo mode safety tests.

These tests do NOT require a running Streamlit server. They inject a
minimal streamlit stub so the public_demo module can be imported,
then exercise the security-critical helpers directly.
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Minimal Streamlit stub. Mirrors the style used in test_smoke.py.
# ---------------------------------------------------------------------------
class _Flex:
    def __init__(self, name=None):
        self._name = name

    def __call__(self, *a, **k):
        if self._name == "button":
            return False
        if self._name == "multiselect":
            return []
        if self._name == "selectbox":
            return ""
        if self._name == "radio":
            return ""
        return _Flex()

    def __enter__(self):
        return self

    def __exit__(self, *e):
        return False

    def __getattr__(self, n):
        return _Flex(n)

    def metric(self, *a, **k):
        return None

    def dataframe(self, *a, **k):
        return None

    def json(self, *a, **k):
        return None

    def download_button(self, *a, **k):
        return None

    def __iter__(self):
        return iter([])


class _Sidebar:
    def __getattr__(self, n):
        return _Flex(n)


class _SessionState(dict):
    def __getattr__(self, k):
        if k in self:
            return self[k]
        return None

    def __setattr__(self, k, v):
        self[k] = v

    def __delattr__(self, k):
        if k in self:
            del self[k]


class _StreamlitStub:
    def __init__(self):
        self.session_state = _SessionState()

    def set_page_config(self, *a, **k):
        return None

    def markdown(self, *a, **k):
        return None

    def header(self, *a, **k):
        return None

    def subheader(self, *a, **k):
        return None

    def caption(self, *a, **k):
        return None

    def info(self, *a, **k):
        return None

    def warning(self, *a, **k):
        return None

    def error(self, *a, **k):
        return None

    def success(self, *a, **k):
        return None

    def spinner(self, *a, **k):
        return _Flex("spinner")

    def rerun(self):
        raise SystemExit(0)

    def columns(self, *a, **k):
        return [_Flex("col"), _Flex("col"), _Flex("col"), _Flex("col")]

    @property
    def sidebar(self):
        return _Sidebar()

    def secrets(self):
        return {}

    def __getattr__(self, n):
        if n == "session_state":
            return self.session_state
        if n == "sidebar":
            return self.sidebar()
        return _Flex(n)


def _install_streamlit_stub():
    """Install a fresh streamlit stub into sys.modules."""
    stub = _StreamlitStub()
    sys.modules["streamlit"] = stub
    return stub


@pytest.fixture
def public_demo(monkeypatch, tmp_path):
    """Import public_demo.py with the streamlit stub and a clean tmp root."""
    _install_streamlit_stub()
    # Force a fresh tmp dir for the session root so tests do not pollute
    # one another or the real /tmp/oilops_public_sessions tree.
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    # Drop any cached module so each test gets a fresh import.
    for mod in list(sys.modules):
        if mod == "public_demo" or mod.startswith("public_demo."):
            del sys.modules[mod]
    if "public_demo" in sys.modules:
        del sys.modules["public_demo"]
    sys.path.insert(0, str(PROJECT_ROOT))
    return importlib.import_module("public_demo")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_public_demo_module_imports_cleanly(public_demo):
    assert public_demo is not None
    assert hasattr(public_demo, "main")


def test_public_demo_disables_openai_key_at_import(public_demo, monkeypatch):
    """Even if OPENAI_API_KEY is set, public_demo pops it from os.environ."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-malicious")
    # Re-run the module-level defensive pop.
    os.environ.pop("OPENAI_API_KEY", None)
    public_demo.__dict__  # touch
    # Simulate what happens on every fresh import:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-malicious")
    importlib.reload(public_demo)
    assert os.environ.get("OPENAI_API_KEY") is None, (
        "public_demo must drop OPENAI_API_KEY from os.environ at import"
    )


def test_public_demo_never_calls_file_uploader(public_demo):
    """File uploads are forbidden in public mode. Scan the source."""
    src = (PROJECT_ROOT / "public_demo.py").read_text(encoding="utf-8")
    assert "st.file_uploader" not in src, (
        "public_demo.py must not call st.file_uploader anywhere"
    )


def test_session_docs_swap_restores_env(public_demo):
    """_swap_docs_root_for_call + _restore_docs_root leaves no residue."""
    new_root = Path(tempfile.gettempdir()) / "swap_target"
    os.environ.pop("OILOPS_DOCS_DIR", None)
    token = public_demo._swap_docs_root_for_call(new_root)
    assert os.environ["OILOPS_DOCS_DIR"] == str(new_root)
    public_demo._restore_docs_root(token)
    assert "OILOPS_DOCS_DIR" not in os.environ


def test_session_docs_swap_restores_previous_value(public_demo):
    """If OILOPS_DOCS_DIR was set, _restore must put the original back."""
    original = "/some/existing/root"
    os.environ["OILOPS_DOCS_DIR"] = original
    try:
        token = public_demo._swap_docs_root_for_call(Path("/tmp/x"))
        assert os.environ["OILOPS_DOCS_DIR"] == str(Path("/tmp/x"))
        public_demo._restore_docs_root(token)
        assert os.environ["OILOPS_DOCS_DIR"] == original
    finally:
        del os.environ["OILOPS_DOCS_DIR"]


def test_classify_expense_called_with_enable_ai_false(public_demo, monkeypatch):
    """The pipeline must always pass enable_ai=False to classify_expense."""
    captured = {}

    def fake_classify(extraction, *, provider=None, enable_ai=True, api_key=None):
        captured["enable_ai"] = enable_ai
        captured["provider"] = provider
        return public_demo.MockProvider().classify(
            public_demo.build_classifier_payload(extraction)
        )

    monkeypatch.setattr(public_demo, "classify_expense", fake_classify)
    pdfs = public_demo._list_synthetic_pdfs()
    if not pdfs:
        pytest.skip("No synthetic PDFs available in data/demo_documents.")
    # Force a session id so session_state is stable.
    public_demo.st.session_state["_public_session_id"] = "test_session_id_01"
    public_demo.st.session_state["_public_seeded"] = True
    result = public_demo._run_pipeline_on_sample(pdfs[0])
    assert captured["enable_ai"] is False
    assert captured["provider"] is not None
    assert isinstance(result, dict)
    assert result.get("status") in {"ready", "failed"}


def test_session_ids_are_isolated(public_demo, monkeypatch):
    """Two different session ids must produce different DB and docs dirs."""
    public_demo.st.session_state["_public_session_id"] = "AAA"
    db_a = str(public_demo._session_db_path())
    docs_a = str(public_demo._session_docs_dir())
    public_demo.st.session_state["_public_session_id"] = "BBB"
    db_b = str(public_demo._session_db_path())
    docs_b = str(public_demo._session_docs_dir())
    assert db_a != db_b
    assert docs_a != docs_b
    assert "AAA" in db_a and "AAA" in docs_a
    assert "BBB" in db_b and "BBB" in docs_b


def test_pipeline_writes_to_session_docs_dir_only(
    public_demo, monkeypatch, tmp_path
):
    """The session's docs dir must receive new PDFs, never the project root."""
    public_demo.st.session_state["_public_session_id"] = "isolated_test"
    public_demo.st.session_state["_public_seeded"] = True
    pdfs = public_demo._list_synthetic_pdfs()
    if not pdfs:
        pytest.skip("No synthetic PDFs available.")
    # Project data/documents must not be touched.
    project_docs = PROJECT_ROOT / "data" / "documents"
    before = set(project_docs.glob("invoice_*.pdf")) if project_docs.exists() else set()
    result = public_demo._run_pipeline_on_sample(pdfs[0])
    after = set(project_docs.glob("invoice_*.pdf")) if project_docs.exists() else set()
    assert before == after, (
        "Pipeline must never write into the project data/documents tree"
    )
    # The session docs dir should now contain the new PDF.
    session_docs = public_demo._session_docs_dir()
    files = list(session_docs.glob("invoice_*.pdf"))
    assert files, "Session docs dir should have received a new PDF"
    # OILOPS_DOCS_DIR must be back to its pre-call value (or unset).
    # The pipeline may leave it unset, which is the safe default.
    assert "OILOPS_DOCS_DIR" not in os.environ or \
        os.environ["OILOPS_DOCS_DIR"] == str(session_docs)
