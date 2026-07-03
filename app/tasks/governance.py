"""Celery tasks for FR-5/FR-6: governance-document ingestion and analysis
dispatch to the Compliance Agent (framework mode) or Governance Agent
(document mode)."""
import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import text

from app.agents.registry import get_agent_graph
from app.core.celery_app import app
from app.services.embeddings import embed_text, to_pgvector
from app.services.github_client import GitHubRemediationClient
from app.tasks.remediation import SyncSessionLocal, _get_github_token_for_org
from app.tasks.standardization import _fetch_workflow_contents

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 1500


def _chunk_text(text_value: str, size: int = _CHUNK_SIZE) -> list[str]:
    return [text_value[i:i + size] for i in range(0, len(text_value), size) if text_value[i:i + size].strip()]


@app.task(bind=True, max_retries=2, default_retry_delay=30)
def extract_governance_requirements_task(self, message: dict) -> dict:
    """Extract structured requirements from an uploaded doc and embed it for retrieval."""
    document_id = uuid.UUID(message["document_id"])
    session = SyncSessionLocal()
    try:
        row = session.execute(
            text("SELECT org_login, doc_type, raw_text FROM governance_documents WHERE id = :id"),
            {"id": str(document_id)},
        ).fetchone()
        if not row:
            return {"status": "not_found", "document_id": str(document_id)}
        org_login, doc_type, raw_text = row

        from app.agents.nodes import _converse, _parse_json

        prompt = (
            f"Extract a structured list of distinct compliance/governance requirements from this "
            f"{doc_type.replace('_', ' ')} document. Respond with ONLY valid JSON:\n"
            '{"requirements": [{"id": "<short id>", "description": "<one sentence>"}]}\n\n'
            f"Document text (truncated to 10000 chars):\n{raw_text[:10000]}"
        )
        parsed = _parse_json(_converse(prompt, max_tokens=2048))
        structured = parsed.get("requirements", [])

        session.execute(
            text("UPDATE governance_documents SET structured_requirements = CAST(:req AS jsonb), updated_at = :now WHERE id = :id"),
            {"id": str(document_id), "req": json.dumps({"requirements": structured}), "now": datetime.now(timezone.utc)},
        )

        # Chunk + embed for the Governance Agent's pgvector retrieval.
        session.execute(
            text("DELETE FROM log_embeddings WHERE source_type = 'governance_doc' AND source_id = :id"),
            {"id": str(document_id)},
        )
        for chunk in _chunk_text(raw_text):
            embedding = embed_text(chunk)
            session.execute(
                text(
                    """
                    INSERT INTO log_embeddings (source_type, source_id, org_login, chunk_text, embedding, metadata)
                    VALUES ('governance_doc', :sid, :org, :chunk, CAST(:emb AS vector), CAST(:meta AS jsonb))
                    """
                ),
                {
                    "sid": str(document_id),
                    "org": org_login,
                    "chunk": chunk,
                    "emb": to_pgvector(embedding),
                    "meta": json.dumps({"doc_type": doc_type}),
                },
            )
        session.commit()

        return {"status": "completed", "document_id": str(document_id), "requirements": len(structured)}

    except Exception as exc:
        logger.exception("Governance requirement extraction failed for %s: %s", document_id, exc)
        raise self.retry(exc=exc)
    finally:
        session.close()


@app.task(bind=True, max_retries=2, default_retry_delay=30)
def run_governance_analysis_task(self, message: dict) -> dict:
    """Diff every workflow file in a repo against a framework (Compliance Agent)
    or an uploaded document (Governance Agent), writing compliance_findings."""
    org_login = message["org_login"]
    repo_name = message["repo_name"]
    ref = message.get("ref") or "main"
    mode = message["mode"]
    framework = message.get("framework")
    document_id = message.get("document_id")

    session = SyncSessionLocal()
    github: GitHubRemediationClient | None = None
    try:
        github_token = _get_github_token_for_org(session, org_login)
        github = GitHubRemediationClient(github_token)
        workflow_contents = _fetch_workflow_contents(github, org_login, repo_name, ref)

        agent_graph = get_agent_graph("compliance" if mode == "framework" else "governance")
        now = datetime.now(timezone.utc)
        finding_count = 0

        for path, content in workflow_contents.items():
            if mode == "framework":
                result = agent_graph.invoke({
                    "repo_owner": org_login,
                    "repo_name": repo_name,
                    "workflow_file": path,
                    "workflow_yaml": content,
                    "framework": framework,
                    "agent_trace": [],
                })
            else:
                result = agent_graph.invoke({
                    "repo_owner": org_login,
                    "repo_name": repo_name,
                    "workflow_file": path,
                    "workflow_yaml": content,
                    "governance_document_id": document_id,
                    "agent_trace": [],
                })

            for finding in result.get("findings", []):
                session.execute(
                    text(
                        """
                        INSERT INTO compliance_findings
                            (id, org_login, repo_name, workflow_file, governance_document_id,
                             requirement_id, status, finding_detail, remediation_suggestion,
                             severity, computed_at)
                        VALUES
                            (:id, :org_login, :repo_name, :workflow_file, :document_id,
                             :requirement_id, :status, :detail, :remediation, :severity, :now)
                        """
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "org_login": org_login,
                        "repo_name": repo_name,
                        "workflow_file": path,
                        "document_id": document_id if mode == "document" else None,
                        "requirement_id": finding.get("requirement_id", "unknown"),
                        "status": finding.get("status", "not_applicable"),
                        "detail": finding.get("detail", ""),
                        "remediation": finding.get("remediation_suggestion") or None,
                        "severity": finding.get("severity", "medium"),
                        "now": now,
                    },
                )
                finding_count += 1

        session.commit()
        return {"status": "completed", "org_login": org_login, "repo_name": repo_name, "findings": finding_count}

    except Exception as exc:
        logger.exception("Governance analysis failed for %s/%s: %s", org_login, repo_name, exc)
        raise self.retry(exc=exc)
    finally:
        session.close()
        if github:
            github.close()
