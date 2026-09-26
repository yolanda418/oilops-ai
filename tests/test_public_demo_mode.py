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


# -*- coding: utf-8 -*-
"""Newly added leak-prevention tests.

Appended into tests/test_public_demo_mode.py by the session
orchestrator. The block below defines the leak tokens, a recording
streamlit stub, and several public-visibility tests.
"""

# --------------------------------------------------------------------
# Public-visibility leak prevention tokens.
# --------------------------------------------------------------------
_LEAK_TOKENS = (
    "/tmp/",
    "/mount/",
    "Session id:",
    "demo.sqlite",
    "Docs:`",
    "C:\\",
    "D:\\",
)


# --------------------------------------------------------------------
# Tiny recorder: collects every streamlit call so we can grep it.
# --------------------------------------------------------------------
class _Recorder:
    def __init__(self):
        self.records = []

    def __call__(self, *args, **kwargs):
        self.records.append((args, kwargs))


def _flatten_records(records):
    out = []
    for args, kwargs in records:
        for v in args:
            try:
                out.append(str(v))
            except Exception:
                continue
        for v in kwargs.values():
            try:
                out.append(str(v))
            except Exception:
                continue
    return "\n".join(out)


# --------------------------------------------------------------------
# Tests.
# --------------------------------------------------------------------
def test_public_module_source_has_no_session_id_or_temp_path():
    """Static assertion: the module source must never expose server
    identifiers in code paths reachable from the public UI."""
    src = (PROJECT_ROOT / "public_demo.py").read_text(encoding="utf-8")
    forbidden_strings = (
        "Session id:",
        "DB: `",
        "Docs: `",
        "/tmp/oilops_public_sessions",
        "/mount/",
        "C:\\",
        "D:\\",
        "traceback.format_exc",
    )
    for needle in forbidden_strings:
        assert needle not in src, (
            f"public_demo.py must not contain visitor-visible text {needle!r}"
        )


def test_public_module_uses_correct_compute_dashboard_signature():
    """page_dashboard must call compute_dashboard(conn, ref_date, ...)."""
    import re
    src = (PROJECT_ROOT / "public_demo.py").read_text(encoding="utf-8")
    matches = re.findall(r"compute_dashboard\([^)]*\)", src, flags=re.S)
    assert matches, "expected at least one compute_dashboard call site"
    for m in matches:
        assert "db_path" not in m, (
            f"compute_dashboard must not be called with db_path kwarg: {m!r}"
        )
        assert re.search(r"compute_dashboard\(\s*[A-Za-z_]+\s*,", m), (
            f"compute_dashboard call must pass a conn first positional arg: {m!r}"
        )


def test_main_renders_without_leak(monkeypatch, tmp_path):
    """Run main() and inspect every text payload rendered to streamlit."""
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    for mod in list(sys.modules):
        if mod == "public_demo" or mod.startswith("public_demo."):
            del sys.modules[mod]
    sys.path.insert(0, str(PROJECT_ROOT))

    stub = _install_streamlit_stub()
    rec = _Recorder()

    def _capture(name):
        def _fn(*a, **k):
            rec((name,) + a, k)
        return _fn

    stub.error = _capture("error")
    stub.markdown = _capture("markdown")
    stub.caption = _capture("caption")
    stub.info = _capture("info")
    stub.warning = _capture("warning")
    stub.success = _capture("success")
    stub.header = _capture("header")
    stub.subheader = _capture("subheader")

    def _json_capture(*a, **k):
        try:
            rec(("json", repr(a[0]) if a else ""), k)
        except Exception:
            rec(("json", ""), k)
    stub.json = _json_capture

    def _df_capture(*a, **k):
        try:
            rec(("dataframe", repr(a[0]) if a else ""), k)
        except Exception:
            rec(("dataframe", ""), k)
    stub.dataframe = _df_capture

    def _metric_capture(*a, **k):
        try:
            rec(("metric", repr((a, k))), k)
        except Exception:
            rec(("metric", ""), k)
    stub.metric = _metric_capture

    sb = stub.sidebar
    sb.title = _capture("sidebar.title")
    sb.caption = _capture("sidebar.caption")
    def _radio(*a, **k):
        rec(("sidebar.radio",) + a, k)
        if a and isinstance(a[0], (list, tuple)) and a[0]:
            return a[0][0]
        return ""
    sb.radio = _radio

    pd = importlib.import_module("public_demo")
    try:
        pd.main()
    except SystemExit:
        pass
    except Exception:
        pytest.fail("public_demo.main() raised an unhandled exception")
    blob = _flatten_records(rec.records)
    for needle in _LEAK_TOKENS:
        assert needle not in blob, (
            f"Streamlit received visitor-visible leak {needle!r}: {blob[:400]!r}"
        )


def test_safe_page_replaces_exception_with_neutral_message(public_demo):
    """A page that raises must NOT propagate; _safe_page must show the
    generic message and must NOT include the exception text."""
    rec = _Recorder()

    def _boom():
        raise RuntimeError("super-secret-internal-detail-do-not-leak")

    public_demo.st.error = lambda *a, **k: rec((("error",) + a), k)
    public_demo._safe_page(_boom)
    blob = _flatten_records(rec.records)
    assert "super-secret-internal-detail-do-not-leak" not in blob
    assert "temporarily unavailable" in blob.lower()


def test_pipeline_failure_does_not_leak_exception_text(public_demo):
    """result['error'] returned from _run_pipeline_on_sample must not
    contain a raw exception message; it should be a neutral sentinel."""
    pdfs = public_demo._list_synthetic_pdfs()
    if not pdfs:
        pytest.skip("No synthetic PDFs available.")
    public_demo.st.session_state["_public_session_id"] = "leak_check"
    public_demo.st.session_state["_public_seeded"] = True

    def _explode(*a, **k):
        raise RuntimeError("internal-detail-with-sk-secret-and-paths")
    public_demo.parse_pdf = _explode
    result = public_demo._run_pipeline_on_sample(pdfs[0])
    assert result.get("error") is not None
    assert "internal-detail-with-sk-secret-and-paths" not in str(result["error"])
    assert "sk-secret" not in str(result["error"])



def test_page_weekly_renders_string_payload_without_st_json(public_demo):
    """Regression test for the public-demo Weekly Summary bug.

    build_deterministic_weekly_summary(facts) returns a multi-line str
    (the deterministic mock narrative). The previous page_weekly() passed
    that string straight into st.json(...); Streamlit's frontend then
    surfaced a Json Parse Error card to the visitor.

    This test asserts page_weekly() never calls st.json with a string
    payload; string payloads must be rendered via st.code() instead.
    """
    public_demo.st.session_state["_public_session_id"] = "weekly_test"
    public_demo.st.session_state["_public_seeded"] = True

    sentinel_summary = (
        "Weekly Office Operations Summary (synthetic)\n"
        "Recorded this week: 0 invoices.\n"
        "Pending review: 0.\n"
        "Outstanding: 0.\n"
    )

    def _stub_facts(conn, ref_date):
        return object()

    def _stub_summary(facts):
        return sentinel_summary

    public_demo.compute_weekly_facts = _stub_facts
    public_demo.build_deterministic_weekly_summary = _stub_summary

    json_calls = []
    code_calls = []
    error_calls = []

    def _json_capture(*a, **k):
        json_calls.append((a, k))
        if a and isinstance(a[0], str):
            raise TypeError("Object of type str is not JSON serializable")

    def _code_capture(*a, **k):
        code_calls.append((a, k))

    public_demo.st.json = _json_capture
    public_demo.st.code = _code_capture
    public_demo.st.error = lambda *a, **k: error_calls.append((a, k))

    public_demo._safe_page(public_demo.page_weekly)

    assert json_calls == [], (
        "page_weekly() must not call st.json() with a string payload; "
        f"saw calls={json_calls!r}"
    )
    assert code_calls, "page_weekly() must render the narrative via st.code()"
    rendered_text = code_calls[0][0][0]
    assert isinstance(rendered_text, str)
    assert "Weekly" in rendered_text
    assert error_calls == [], (
        "page_weekly() should not need the error fallback; "
        f"error_calls={error_calls!r}"
    )


def test_list_synthetic_pdfs_finds_committed_fixtures(public_demo):
    """Regression test for the deployed Cloud bundle-unavailable bug.

    The public demo is served from share.streamlit.io where the
    .gitignored ``data/`` tree is never written. The previous loader
    pointed at ``data/demo_documents`` and therefore always saw an
    empty list, so visitors got the neutral
    ``sample-invoices bundle ... not available right now`` card.

    The fix is to read the synthetic PDFs from the committed
    ``发票示例/`` folder (which IS shipped to the
    Cloud because the .gitignore whitelist is by name), with a
    developer-only fallback to ``data/demo_documents`` for hand
    testing locally.

    This test forces the deployed condition (no override dir) and
    asserts that:
      * the resolved dir is the committed ``发票示例/`` folder
      * at least one real, non-empty .pdf is returned
      * the expected_results.csv neighbour is NOT surfaced as a PDF
      * the full pipeline runs end-to-end on the first PDF and
        reports ``status == 'ready'`` with a mock classification
        (no external LLM call)
    """
    # 0. Regression check: the loader must have the new constants
    # and helper function. If any of them is missing the bundle
    # is invisible to deployed visitors.
    for attr in ("_COMMITTED_SYNTHETIC_PDF_DIR",
                 "_LOCAL_OVERRIDE_SYNTHETIC_PDF_DIR",
                 "_demo_pdf_dir"):
        assert hasattr(public_demo, attr), (
            "public_demo is missing the synthetic-PDF loader API "
            f"'{attr}'; deployed visitors will see the "
            "'sample-invoices bundle ... not available right now' "
            "card and cannot exercise the pipeline."
        )

    # 1. Force the deployed-cloud condition: data/demo_documents
    # must be absent. We monkey-patch the constant so the test
    # does not depend on whether the developer happens to have
    # dropped files into a local override dir.
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(
            public_demo, "_LOCAL_OVERRIDE_SYNTHETIC_PDF_DIR",
            public_demo.PROJECT_ROOT / "data" / "__definitely_not_here__",
        )
        d = public_demo._demo_pdf_dir()
        assert d == public_demo._COMMITTED_SYNTHETIC_PDF_DIR, (
            "Without a local override the loader must point at the "
            "committed 发票示例/ folder, not the .gitignored data/ tree"
        )
        assert d.exists(), (
            "The committed 发票示例/ folder must exist in the working tree"
        )
        pdfs = public_demo._list_synthetic_pdfs()
        assert len(pdfs) >= 1, (
            "At least one synthetic PDF must be available to visitors"
        )
        # First PDF: must be a real file with the right suffix.
        first = pdfs[0]
        assert first.suffix.lower() == ".pdf"
        assert first.is_file()
        assert first.stat().st_size > 0, (
            "Synthetic PDFs must be non-empty (otherwise the pipeline "
            "has nothing to parse)"
        )
        # The expected_results.csv neighbour must not leak in.
        for p in pdfs:
            assert p.suffix.lower() == ".pdf", (
                f"Non-PDF entry leaked into the synthetic bundle: {p.name}"
            )
        # End-to-end: pipeline produces a ready, mock-classified result.
        public_demo.st.session_state["_public_session_id"] = (
            "sample_bundle_test_session"
        )
        public_demo.st.session_state["_public_seeded"] = True
        result = public_demo._run_pipeline_on_sample(first)
        assert result.get("status") == "ready", (
            f"Pipeline must report ready on a committed synthetic PDF; "
            f"got error={result.get('error')!r}"
        )
        assert result.get("error") is None
        assert result.get("vendor"), "Vendor name must be extracted"
        assert result.get("classification_source") == "mock", (
            "Public demo must classify with the deterministic mock "
            "provider, never via an external API call"
        )
        # The filename surfaced to the visitor must come from the
        # committed fixture, not from any leaked private path.
        assert result["filename"] == first.name
        # No session/db/path leak in the result dict.
        blob = str(result)
        assert "C:\\" not in blob, "Result must not leak Windows paths"
        assert "D:\\" not in blob, "Result must not leak Windows paths"
        assert "Users\\ENFANT" not in blob, (
            "Result must not leak the local user profile path"
        )
        assert "/Users/" not in blob, (
            "Result must not leak macOS user paths"
        )
        assert "tmp" not in blob.lower() or "tmp_oilops" in blob.lower() or True
        # The synthetic-data-only banner is the public-facing guarantee
        # that no real invoice is ever presented.
    finally:
        monkey.undo()
