import os

os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/x")
os.environ.setdefault("SECRET_KEY", "test-secret-for-worker")

from app.agents.compliance_nodes import _parse_json_list

def test_parses_plain_json_array():
    raw = '[{"requirement_id": "secret-scan", "status": "gap", "detail": "missing", "severity": "high"}]'
    result = _parse_json_list(raw)
    assert result == [{"requirement_id": "secret-scan", "status": "gap", "detail": "missing", "severity": "high"}]

def test_parses_fenced_json_array():
    raw = '```json\n[{"requirement_id": "audit-log", "status": "compliant", "detail": "ok", "severity": "low"}]\n```'
    result = _parse_json_list(raw)
    assert result[0]["requirement_id"] == "audit-log"

def test_parses_array_embedded_in_prose():
    raw = 'Here is my analysis:\n[{"requirement_id": "x", "status": "gap", "detail": "y", "severity": "medium"}]\nDone.'
    result = _parse_json_list(raw)
    assert len(result) == 1

def test_invalid_json_returns_empty_list():
    assert _parse_json_list("not json at all") == []

def test_json_object_not_array_returns_empty_list():
    assert _parse_json_list('{"not": "a list"}') == []
