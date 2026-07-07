import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import text

from app.agents.registry import get_agent_graph
from app.core.celery_app import app
from app.services.github_client import GitHubRemediationClient
from app.tasks.agent_report import record_agent_run
from app.tasks.remediation import SyncSessionLocal, _get_github_token_for_org, _publish_event

logger = logging.getLogger(__name__)

@app.task(bind=True, max_retries=2, default_retry_delay=30)
def process_pull_request(self, message: dict) -> dict:
    repo_owner: str = message["repo_owner"]
    repo_name: str = message["repo_name"]
    pr_number: int = message["pr_number"]

    session = SyncSessionLocal()
    github: GitHubRemediationClient | None = None
    review_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    try:
        session.execute(
            text(
                """
                INSERT INTO pr_reviews
                    (id, org_login, repo_name, application_id, pr_number, pr_url, status, created_at, updated_at)
                VALUES
                    (:id, :org_login, :repo_name,
                     (SELECT application_id FROM application_repos WHERE org_login = :org_login AND repo_name = :repo_name),
                     :pr_number, :pr_url, 'analyzing', :now, :now)
                """
            ),
            {
                "id": str(review_id),
                "org_login": repo_owner,
                "repo_name": repo_name,
                "pr_number": pr_number,
                "pr_url": message.get("pr_url", ""),
                "now": now,
            },
        )
        session.commit()
        _publish_event("pr_review_created", {"id": str(review_id), "status": "analyzing"})

        github_token = _get_github_token_for_org(session, repo_owner)
        github = GitHubRemediationClient(github_token)
        author = message.get("author") or github.get_pull_request_author(repo_owner, repo_name, pr_number)
        diff = github.get_pull_request_diff(repo_owner, repo_name, pr_number)

        peer_review_graph = get_agent_graph("peer_review")
        final_state = peer_review_graph.invoke({
            "repo_owner": repo_owner,
            "repo_name": repo_name,
            "pr_number": pr_number,
            "pr_title": message.get("pr_title", ""),
            "diff": diff,
            "agent_trace": [],
        })

        session.execute(
            text(
                """
                UPDATE pr_reviews SET
                    status = 'completed',
                    author = :author,
                    risk_score = :risk_score,
                    findings = :findings,
                    review_summary = :review_summary,
                    agent_trace = :agent_trace,
                    updated_at = :now
                WHERE id = :id
                """
            ),
            {
                "id": str(review_id),
                "author": author,
                "risk_score": final_state.get("risk_score", 0),
                "findings": final_state.get("findings", []),
                "review_summary": final_state.get("review_summary", ""),
                "agent_trace": final_state.get("agent_trace", []),
                "now": datetime.now(timezone.utc),
            },
        )
        session.commit()
        _publish_event("pr_review_updated", {"id": str(review_id), "status": "completed"})

        findings = final_state.get("findings", [])
        record_agent_run(
            session,
            org_login=repo_owner,
            repo_name=repo_name,
            agent_name="peer_review",
            outcome="needs_review" if findings else "success",
            summary=(
                f"{len(findings)} finding(s) on PR #{pr_number} in {repo_name} (risk {final_state.get('risk_score', 0)}/10)."
                if findings else f"No governance findings on PR #{pr_number} in {repo_name}."
            ),
            gaps_found=len(findings),
        )
        session.commit()

        return {"status": "completed", "review_id": str(review_id)}

    except Exception as exc:
        logger.exception("Peer review failed for PR #%s in %s/%s: %s", pr_number, repo_owner, repo_name, exc)
        try:
            session.execute(
                text("UPDATE pr_reviews SET status = 'failed', updated_at = :now WHERE id = :id"),
                {"id": str(review_id), "now": datetime.now(timezone.utc)},
            )
            session.commit()
        except Exception:
            logger.exception("Failed to mark PR review %s as failed", review_id)
        raise self.retry(exc=exc)

    finally:
        session.close()
        if github:
            github.close()
