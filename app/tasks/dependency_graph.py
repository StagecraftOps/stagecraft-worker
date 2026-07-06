import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import text

from app.analysis.graph_builder import build_graph_data, persist_graph
from app.core.celery_app import app
from app.services.github_client import GitHubRemediationClient
from app.tasks.remediation import SyncSessionLocal, _get_github_token_for_org

logger = logging.getLogger(__name__)

@app.task(bind=True, max_retries=2, default_retry_delay=30)
def build_dependency_graph_task(self, message: dict) -> dict:
    graph_id = uuid.UUID(message["graph_id"])
    org_login = message["org_login"]
    repo_name = message["repo_name"]
    ref = message.get("ref") or "main"

    session = SyncSessionLocal()
    github: GitHubRemediationClient | None = None
    try:
        session.execute(
            text("UPDATE graphs SET status = 'building', updated_at = :now WHERE id = :id"),
            {"id": str(graph_id), "now": datetime.now(timezone.utc)},
        )
        session.commit()

        github_token = _get_github_token_for_org(session, org_login)
        github = GitHubRemediationClient(github_token)

        logger.info("Building dependency graph for %s/%s@%s", org_login, repo_name, ref)
        nodes, edges = build_graph_data(github, org_login, repo_name, ref)
        persist_graph(session, graph_id, org_login, repo_name, nodes, edges)

        logger.info(
            "Dependency graph %s completed: %d nodes, %d edges", graph_id, len(nodes), len(edges)
        )

        from app.tasks.knowledge_graph import build_knowledge_graph_task
        build_knowledge_graph_task.delay({"org_login": org_login})

        return {"status": "completed", "graph_id": str(graph_id), "nodes": len(nodes), "edges": len(edges)}

    except Exception as exc:
        logger.exception("Dependency graph build failed for %s/%s: %s", org_login, repo_name, exc)
        try:
            session.execute(
                text(
                    "UPDATE graphs SET status = 'failed', error_message = :err, updated_at = :now WHERE id = :id"
                ),
                {"id": str(graph_id), "err": str(exc)[:2048], "now": datetime.now(timezone.utc)},
            )
            session.commit()
        except Exception:
            logger.exception("Failed to mark graph %s as failed", graph_id)
        raise self.retry(exc=exc)

    finally:
        session.close()
        if github:
            github.close()
