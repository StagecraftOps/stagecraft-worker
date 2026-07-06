import logging

from app.analysis.knowledge_graph_builder import build_knowledge_graph
from app.core.celery_app import app
from app.tasks.remediation import SyncSessionLocal

logger = logging.getLogger(__name__)

@app.task(bind=True, max_retries=2, default_retry_delay=30)
def build_knowledge_graph_task(self, message: dict) -> dict:
    org_login = message["org_login"]
    session = SyncSessionLocal()
    try:
        graph_id, node_count, edge_count = build_knowledge_graph(session, org_login)
        logger.info("Knowledge graph built for %s: %d nodes, %d edges", org_login, node_count, edge_count)
        return {"status": "completed", "graph_id": str(graph_id), "nodes": node_count, "edges": edge_count}
    except Exception as exc:
        logger.exception("Knowledge graph build failed for %s: %s", org_login, exc)
        raise self.retry(exc=exc)
    finally:
        session.close()
