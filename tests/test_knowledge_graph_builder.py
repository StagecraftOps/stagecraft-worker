import os
import uuid
from unittest.mock import MagicMock

os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/x")
os.environ.setdefault("SECRET_KEY", "test-secret-for-worker")

from app.analysis.knowledge_graph_builder import _add_edge, _failure_display_name, _upsert_node

def test_failure_display_name_uses_specific_category():
    assert _failure_display_name("DEPENDENCY_VERSION", "pip install failed") == "DEPENDENCY_VERSION"

def test_failure_display_name_falls_back_to_root_cause_when_unknown():
    assert _failure_display_name("UNKNOWN", "pip could not find pyproject.toml at the expected path") == \
        "pip could not find pyproject.toml at the expected path"

def test_failure_display_name_unknown_is_case_insensitive():
    assert _failure_display_name("unknown", "some root cause") == "some root cause"

def test_failure_display_name_truncates_long_root_cause():
    long_cause = "x" * 200
    result = _failure_display_name(None, long_cause)
    assert result == long_cause[:60]
    assert len(result) == 60

def test_failure_display_name_no_category_no_root_cause():
    assert _failure_display_name(None, None) == "Unclassified failure"
    assert _failure_display_name("UNKNOWN", "") == "Unclassified failure"

def test_upsert_node_returns_existing_id_when_found():
    session = MagicMock()
    existing_id = uuid.uuid4()
    session.execute.return_value.fetchone.return_value = (existing_id,)

    graph_id = uuid.uuid4()
    result = _upsert_node(session, graph_id, "governance_rule", "governance_rule::secret-scan", "secret-scan")

    assert result == existing_id

    assert session.execute.call_count == 1

def test_upsert_node_inserts_when_not_found():
    session = MagicMock()
    session.execute.return_value.fetchone.return_value = None

    graph_id = uuid.uuid4()
    result = _upsert_node(session, graph_id, "failure", "failure::abc", "DEPENDENCY_VERSION")

    assert isinstance(result, uuid.UUID)

    assert session.execute.call_count == 2
    insert_call = session.execute.call_args_list[1]
    params = insert_call.args[1]
    assert params["ntype"] == "failure"
    assert params["key"] == "failure::abc"
    assert params["id"] == str(result)

def test_add_edge_passes_correct_edge_type():
    session = MagicMock()
    graph_id = uuid.uuid4()
    source_id = uuid.uuid4()
    target_id = uuid.uuid4()

    _add_edge(session, graph_id, source_id, target_id, "governs")

    assert session.execute.call_count == 1
    params = session.execute.call_args.args[1]
    assert params["etype"] == "governs"
    assert params["src"] == str(source_id)
    assert params["tgt"] == str(target_id)
