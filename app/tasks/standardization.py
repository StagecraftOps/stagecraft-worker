"""Celery tasks for FR-3 (template adoption diff) and FR-4 (repeated-pattern discovery)."""
import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import text

from app.analysis.graph_builder import _list_workflow_files
from app.analysis.pattern_frequency import find_near_miss_groups, find_repeated_patterns
from app.analysis.template_diff import diff_workflow_against_template
from app.core.celery_app import app
from app.services.bedrock_client import BedrockRemediationClient
from app.services.github_client import GitHubRemediationClient
from app.tasks.remediation import SyncSessionLocal, _get_github_token_for_org

logger = logging.getLogger(__name__)


def _fetch_workflow_contents(
    github: GitHubRemediationClient, owner: str, repo: str, ref: str
) -> dict[str, str]:
    tree = github.get_repo_tree(owner, repo, ref)
    contents: dict[str, str] = {}
    for path in _list_workflow_files(tree):
        content = github.get_file_content(owner, repo, path, ref)
        if content is not None:
            contents[path] = content
    return contents


@app.task(bind=True, max_retries=2, default_retry_delay=30)
def run_template_diff_task(self, message: dict) -> dict:
    """Diff every workflow file in a repo against every active template for its org."""
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
            return {"status": "no_templates", "org_login": org_login}

        github_token = _get_github_token_for_org(session, org_login)
        github = GitHubRemediationClient(github_token)
        workflow_contents = _fetch_workflow_contents(github, org_login, repo_name, ref)
        bedrock = BedrockRemediationClient()

        now = datetime.now(timezone.utc)
        diff_count = 0
        for path, content in workflow_contents.items():
            for template_id, template_name, template_yaml in templates:
                diff = diff_workflow_against_template(content, template_yaml)

                # LLM layer, FR-3: WHY the gap matters, not just WHAT it is --
                # skipped for fully-compliant diffs (nothing to judge) and
                # best-effort otherwise (a Bedrock hiccup shouldn't fail the
                # whole structural analysis, which has already succeeded).
                if diff["missing_components"] or diff["version_drift"]:
                    try:
                        diff["narrative"] = bedrock.narrate_template_diff(diff, path, template_name)
                    except Exception as narrate_exc:
                        logger.warning(
                            "Template-diff narration failed for %s (template %s): %s",
                            path, template_name, narrate_exc,
                        )

                session.execute(
                    text(
                        """
                        INSERT INTO template_diffs
                            (id, org_login, repo_name, workflow_file, template_id,
                             diff_summary, adoption_score, computed_at)
                        VALUES
                            (:id, :org_login, :repo_name, :workflow_file, :template_id,
                             CAST(:diff_summary AS jsonb), :adoption_score, :computed_at)
                        """
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "org_login": org_login,
                        "repo_name": repo_name,
                        "workflow_file": path,
                        "template_id": str(template_id),
                        "diff_summary": json.dumps(diff),
                        "adoption_score": diff["adoption_score"],
                        "computed_at": now,
                    },
                )
                diff_count += 1
        session.commit()

        return {"status": "completed", "org_login": org_login, "repo_name": repo_name, "diffs": diff_count}

    except Exception as exc:
        logger.exception("Template diff failed for %s/%s: %s", org_login, repo_name, exc)
        raise self.retry(exc=exc)
    finally:
        session.close()
        if github:
            github.close()


@app.task(bind=True, max_retries=2, default_retry_delay=30)
def run_pattern_frequency_task(self, message: dict) -> dict:
    """Find component patterns repeated across a repo's workflow files."""
    org_login = message["org_login"]
    repo_name = message["repo_name"]
    ref = message.get("ref") or "main"
    min_occurrences = message.get("min_occurrences", 3)

    session = SyncSessionLocal()
    github: GitHubRemediationClient | None = None
    try:
        github_token = _get_github_token_for_org(session, org_login)
        github = GitHubRemediationClient(github_token)
        workflow_contents = _fetch_workflow_contents(github, org_login, repo_name, ref)

        clusters = find_repeated_patterns(workflow_contents, min_occurrences=min_occurrences)

        # LLM layer, FR-4: exact-hash clustering above only ever catches
        # byte-identical component signatures, so two jobs doing the same
        # thing with one extra or reordered step never cluster. Judge the
        # near-miss groups it missed and, for genuine matches, get a drafted
        # reusable-workflow YAML -- something an exact-hash cluster never
        # produces (it only reports "these N files share this signature").
        # Best-effort per group: one bad Bedrock call shouldn't drop every
        # other group already confirmed in this run.
        bedrock = BedrockRemediationClient()
        near_miss_groups = find_near_miss_groups(workflow_contents, clusters, min_occurrences=min_occurrences)
        for group in near_miss_groups:
            try:
                verdict = bedrock.judge_pattern_cluster(group, min_occurrences)
            except Exception as judge_exc:
                logger.warning("Pattern-cluster LLM judgment failed for a candidate group: %s", judge_exc)
                continue
            if not verdict:
                continue
            files = sorted({j["job_key"].split("::")[0] for j in group})
            all_components = sorted({c for j in group for c in j["components"]})
            clusters.append({
                "pattern_hash": hashlib.sha256(f"semantic::{verdict['pattern_name']}".encode()).hexdigest(),
                "pattern_signature": {
                    "components": all_components,
                    "match_type": "semantic",
                    "pattern_name": verdict["pattern_name"],
                    "draft_template_yaml": verdict["draft_template_yaml"],
                },
                "occurrence_count": len(group),
                "example_workflow_files": files[:5],
            })

        now = datetime.now(timezone.utc)
        # Replace this org's prior pattern-cluster results with the fresh run.
        session.execute(text("DELETE FROM pattern_clusters WHERE org_login = :org"), {"org": org_login})
        for cluster in clusters:
            session.execute(
                text(
                    """
                    INSERT INTO pattern_clusters
                        (id, org_login, pattern_hash, pattern_signature,
                         occurrence_count, example_workflow_files, computed_at)
                    VALUES
                        (:id, :org_login, :pattern_hash, CAST(:pattern_signature AS jsonb),
                         :occurrence_count, :example_workflow_files, :computed_at)
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "org_login": org_login,
                    "pattern_hash": cluster["pattern_hash"],
                    "pattern_signature": json.dumps(cluster["pattern_signature"]),
                    "occurrence_count": cluster["occurrence_count"],
                    "example_workflow_files": cluster["example_workflow_files"],
                    "computed_at": now,
                },
            )
        session.commit()

        return {"status": "completed", "org_login": org_login, "patterns_found": len(clusters)}

    except Exception as exc:
        logger.exception("Pattern frequency analysis failed for %s/%s: %s", org_login, repo_name, exc)
        raise self.retry(exc=exc)
    finally:
        session.close()
        if github:
            github.close()
