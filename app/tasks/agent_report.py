import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import text

logger = logging.getLogger(__name__)

def record_agent_run(
    session,
    *,
    org_login: str,
    agent_name: str,
    outcome: str,
    repo_name: str | None = None,
    github_run_id: str | None = None,
    summary: str | None = None,
    gaps_found: int = 0,
    prs_opened: list[str] | None = None,
    artifacts: list[str] | None = None,
    conditions_evaluated: list[dict] | None = None,
    evidence: dict | None = None,
) -> str:
    run_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    session.execute(
        text(
            """
            INSERT INTO agent_runs
                (id, org_login, repo_name, application_id, agent_name, github_run_id, outcome, summary,
                 gaps_found, prs_opened, artifacts, conditions_evaluated, evidence,
                 created_at, updated_at)
            VALUES
                (:id, :org_login, :repo_name,
                 (SELECT application_id FROM application_repos WHERE org_login = :org_login AND repo_name = :repo_name),
                 :agent_name, :github_run_id, :outcome, :summary,
                 :gaps_found, :prs_opened, :artifacts,
                 CAST(:conditions_evaluated AS jsonb), CAST(:evidence AS jsonb),
                 :created_at, :updated_at)
            """
        ),
        {
            "id": run_id,
            "org_login": org_login,
            "repo_name": repo_name,
            "agent_name": agent_name,
            "github_run_id": github_run_id,
            "outcome": outcome,
            "summary": summary,
            "gaps_found": gaps_found,
            "prs_opened": prs_opened,
            "artifacts": artifacts,
            "conditions_evaluated": json.dumps(conditions_evaluated) if conditions_evaluated is not None else None,
            "evidence": json.dumps(evidence) if evidence is not None else None,
            "created_at": now,
            "updated_at": now,
        },
    )
    return run_id
