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
    from app.tasks.remediation import enqueue_knowledge_graph_rebuild

    mock_task = MagicMock()
    mock_task.delay.side_effect = RuntimeError("broker unavailable")
    with patch("app.tasks.knowledge_graph.build_knowledge_graph_task", mock_task):
        enqueue_knowledge_graph_rebuild("acme")
