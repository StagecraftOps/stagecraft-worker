import logging

from sqlalchemy import text

from app.analysis.template_diff import diff_workflow_against_template
from app.core.celery_app import app
from app.services.github_client import GitHubRemediationClient
from app.tasks.agent_report import record_agent_run
from app.tasks.remediation import SyncSessionLocal, _get_github_token_for_org
from app.tasks.standardization import _fetch_workflow_contents

logger = logging.getLogger(__name__)

_HIGH_RISK_TIERS = {"critical", "high"}

def _load_app_context(session, org_login: str, repo_name: str) -> dict | None:
    row = session.execute(
        text(
            """
            SELECT risk_tier, regulatory_scope
            FROM application_contexts
            WHERE org_login = :org AND repo_name = :repo
            """
        ),
        {"org": org_login, "repo": repo_name},
    ).fetchone()
    if not row:
        return None
    return {"risk_tier": row[0], "regulatory_scope": row[1] or []}

def _classify_drift(diff: dict, app_context: dict | None) -> dict | None:
    drift_types: list[str] = []
    if diff["missing_components"]:
        drift_types.append("MISSING_STAGE")
    for entry in diff["version_drift"]:
        tv = entry.get("template_version") or ""
        wv = entry.get("workflow_version") or ""
        if tv and wv and wv < tv:
            drift_types.append("VERSION_DOWNGRADE")
        else:
            drift_types.append("VERSION_DRIFT")

    if not drift_types:
        return None

    if "MISSING_STAGE" in drift_types or "VERSION_DOWNGRADE" in drift_types:
        severity = "high"
    else:
        severity = "medium"

    if app_context and app_context.get("risk_tier") in _HIGH_RISK_TIERS:
        severity = "critical" if severity == "high" else "high"

    return {
        "drift_types": sorted(set(drift_types)),
        "severity": severity,
        "adoption_score": diff["adoption_score"],
        "missing_components": diff["missing_components"],
        "version_drift": diff["version_drift"],
    }

@app.task(bind=True, max_retries=2, default_retry_delay=30)
def run_drift_detection_task(self, message: dict) -> dict:
    org_login = message["org_login"]
    repo_name = message["repo_name"]
    ref = message.get("ref") or "main"

    session = SyncSessionLocal()
    github: GitHubRemediationClient | None = None
    try:
        templates = session.execute(
            text("SELECT id, name, template_yaml FROM workflow_templates WHERE org_login = :org AND is_active = true"),
            {"org": org_login},
        ).fetchall()
        if not templates:
            record_agent_run(
                session,
                org_login=org_login,
                repo_name=repo_name,
                agent_name="drift_detector",
                outcome="no_action",
                summary="No approved templates registered for this org; nothing to compare against.",
            )
            session.commit()
            return {"status": "no_templates", "org_login": org_login}

        github_token = _get_github_token_for_org(session, org_login)
        github = GitHubRemediationClient(github_token)
        workflow_contents = _fetch_workflow_contents(github, org_login, repo_name, ref)
        app_context = _load_app_context(session, org_login, repo_name)

        findings: list[dict] = []
        for path, content in workflow_contents.items():
            best: dict | None = None
            for _template_id, template_name, template_yaml in templates:
                diff = diff_workflow_against_template(content, template_yaml)
                classified = _classify_drift(diff, app_context)
                if classified is None:
                    continue
                candidate = {"workflow_file": path, "template": template_name, **classified}
                if best is None or candidate["adoption_score"] > best["adoption_score"]:
                    best = candidate
            if best is not None:
                findings.append(best)

        severities = [f["severity"] for f in findings]
        if any(s == "critical" for s in severities):
            outcome = "needs_review"
        elif findings:
            outcome = "needs_review"
        else:
            outcome = "success"

        if findings:
            worst = max(findings, key=lambda f: f["adoption_score"] * -1)
            summary = (
                f"{len(findings)} workflow(s) drifted from approved templates in {repo_name}; "
                f"lowest adoption {min(f['adoption_score'] for f in findings)}%."
            )
        else:
            summary = f"All workflows in {repo_name} align with approved templates."

        record_agent_run(
            session,
            org_login=org_login,
            repo_name=repo_name,
            agent_name="drift_detector",
            outcome=outcome,
            summary=summary,
            gaps_found=len(findings),
            conditions_evaluated=[
                {"name": "workflows_match_approved_templates", "passed": not findings},
                {"name": "no_version_downgrades", "passed": all("VERSION_DOWNGRADE" not in f["drift_types"] for f in findings)},
                {"name": "no_missing_required_stages", "passed": all("MISSING_STAGE" not in f["drift_types"] for f in findings)},
            ],
            evidence={"findings": findings, "ref": ref, "risk_tier": (app_context or {}).get("risk_tier")},
        )
        session.commit()

        return {"status": "completed", "org_login": org_login, "repo_name": repo_name, "drifted": len(findings)}

    except Exception as exc:
        logger.exception("Drift detection failed for %s/%s: %s", org_login, repo_name, exc)
        raise self.retry(exc=exc)
    finally:
        session.close()
        if github:
            github.close()
