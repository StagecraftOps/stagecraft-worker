"""Tests for enqueue_knowledge_graph_rebuild -- the best-effort helper that
keeps the knowledge graph's Failure/GovernanceRule/RuntimeMetric nodes from
going stale between manual Rebuilds, called from remediation.py/governance.py/
optimization.py whenever they write a row the knowledge graph would care
about (see stagecraft_worker/app/tasks/remediation.py)."""
import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/x")
os.environ.setdefault("SECRET_KEY", "test-secret-only")


def test_enqueue_knowledge_graph_rebuild_delays_the_task():
    from app.tasks.remediation import enqueue_knowledge_graph_rebuild

    mock_task = MagicMock()
    with patch("app.tasks.knowledge_graph.build_knowledge_graph_task", mock_task):
        enqueue_knowledge_graph_rebuild("acme")

    mock_task.delay.assert_called_once_with({"org_login": "acme"})


def test_enqueue_knowledge_graph_rebuild_swallows_errors():
    """A queueing hiccup (e.g. broker down) must never bubble up and fail
    the caller's remediation/governance/optimization task over it."""
    from app.tasks.remediation import enqueue_knowledge_graph_rebuild

    mock_task = MagicMock()
    mock_task.delay.side_effect = RuntimeError("broker unavailable")
    with patch("app.tasks.knowledge_graph.build_knowledge_graph_task", mock_task):
        enqueue_knowledge_graph_rebuild("acme")  # must not raise
