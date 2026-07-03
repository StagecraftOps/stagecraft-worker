"""Celery task for FR-7/FR-8/FR-9: bottleneck detection, optimization
recommendations, and future-state simulation for one workflow file.

Reuses FR-1's stored dependency graph (graph_nodes/graph_edges) and FR-2's
job timing tables (job_runs/critical_path_results) directly via raw SQL —
same DB, same pattern the worker already uses for every other table it
doesn't own an ORM model for.
"""
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import text

from app.agents.registry import get_agent_graph
from app.analysis.bottleneck_detector import detect_bottlenecks
from app.analysis.parallelization_advisor import find_parallelization_candidates
from app.core.celery_app import app
from app.services.github_client import GitHubRemediationClient
from app.tasks.remediation import SyncSessionLocal, _get_github_token_for_org

logger = logging.getLogger(__name__)


def _job_name_from_key(external_key: str) -> str:
    # job external_key format: "job::<workflow_file>::<job_id>"
    return external_key.rsplit("::", 1)[-1]


@app.task(bind=True, max_retries=2, default_retry_delay=30)
def run_optimization_analysis_task(self, message: dict) -> dict:
    org_login = message["org_login"]
    repo_name = message["repo_name"]
    workflow_file = message["workflow_file"]
    ref = message.get("ref") or "main"

    session = SyncSessionLocal()
    github: GitHubRemediationClient | None = None
    try:
        graph_row = session.execute(
            text(
                """
                SELECT id FROM graphs
                WHERE org_login = :org AND repo_name = :repo AND graph_type = 'dependency' AND status = 'completed'
                ORDER BY built_at DESC LIMIT 1
                """
            ),
            {"org": org_login, "repo": repo_name},
        ).fetchone()
        if not graph_row:
            return {"status": "no_graph", "org_login": org_login, "repo_name": repo_name}
        graph_id = graph_row[0]

        needs_rows = session.execute(
            text(
                """
                SELECT src.external_key, tgt.external_key
                FROM graph_edges e
                JOIN graph_nodes src ON src.id = e.source_node_id
                JOIN graph_nodes tgt ON tgt.id = e.target_node_id
                WHERE e.graph_id = :graph_id AND e.edge_type = 'needs'
                  AND src.workflow_file = :wf AND tgt.workflow_file = :wf
                """
            ),
            {"graph_id": str(graph_id), "wf": workflow_file},
        ).fetchall()
        needs_edges = [(_job_name_from_key(s), _job_name_from_key(t)) for s, t in needs_rows]

        output_rows = session.execute(
            text(
                """
                SELECT src.external_key, tgt.external_key
                FROM graph_edges e
                JOIN graph_nodes src ON src.id = e.source_node_id
                JOIN graph_nodes tgt ON tgt.id = e.target_node_id
                WHERE e.graph_id = :graph_id AND e.edge_type = 'needs_output'
                  AND src.workflow_file = :wf AND tgt.workflow_file = :wf
                """
            ),
            {"graph_id": str(graph_id), "wf": workflow_file},
        ).fetchall()
        needs_output_edges = [(_job_name_from_key(s), _job_name_from_key(t)) for s, t in output_rows]

        # Historical job durations across the org (for the bottleneck baseline)
        # and the most recent run's per-job durations + critical path.
        history_rows = session.execute(
            text(
                """
                SELECT jr.job_name, jr.duration_seconds
                FROM job_runs jr
                JOIN workflow_runs wr ON wr.id = jr.workflow_run_id
                WHERE wr.org_login = :org AND wr.repo_name = :repo AND wr.workflow_file = :wf
                  AND jr.duration_seconds IS NOT NULL
                """
            ),
            {"org": org_login, "repo": repo_name, "wf": workflow_file},
        ).fetchall()
        historical_durations: dict[str, list[int]] = {}
        for job_name, duration in history_rows:
            historical_durations.setdefault(job_name, []).append(duration)

        latest_run_row = session.execute(
            text(
                """
                SELECT id FROM workflow_runs
                WHERE org_login = :org AND repo_name = :repo AND workflow_file = :wf
                ORDER BY created_at DESC LIMIT 1
                """
            ),
            {"org": org_login, "repo": repo_name, "wf": workflow_file},
        ).fetchone()
        if not latest_run_row:
            return {"status": "no_runs", "org_login": org_login, "repo_name": repo_name}
        latest_run_id = latest_run_row[0]

        current_job_rows = session.execute(
            text("SELECT job_name, duration_seconds FROM job_runs WHERE workflow_run_id = :run_id"),
            {"run_id": str(latest_run_id)},
        ).fetchall()
        current_jobs = [{"job_name": n, "duration_seconds": d or 0} for n, d in current_job_rows]
        job_durations = {j["job_name"]: j["duration_seconds"] for j in current_jobs}

        cp_row = session.execute(
            text(
                """
                SELECT cp.critical_path_job_ids FROM critical_path_results cp
                WHERE cp.workflow_run_id = :run_id
                """
            ),
            {"run_id": str(latest_run_id)},
        ).fetchone()
        critical_path_job_names: list[str] = []
        if cp_row and cp_row[0]:
            name_rows = session.execute(
                text("SELECT id, job_name FROM job_runs WHERE id = ANY(:ids)"),
                {"ids": cp_row[0]},
            ).fetchall()
            id_to_name = {str(i): n for i, n in name_rows}
            critical_path_job_names = [id_to_name[str(jid)] for jid in cp_row[0] if str(jid) in id_to_name]

        bottlenecks = detect_bottlenecks(current_jobs, historical_durations, critical_path_job_names)
        parallelization_candidates = find_parallelization_candidates(needs_edges, needs_output_edges)

        github_token = _get_github_token_for_org(session, org_login)
        github = GitHubRemediationClient(github_token)
        run_row = session.execute(
            text("SELECT head_sha FROM workflow_runs WHERE id = :id"), {"id": str(latest_run_id)}
        ).fetchone()
        head_sha = run_row[0] if run_row else ref
        workflow_yaml = github.get_workflow_yaml(org_login, repo_name, workflow_file, head_sha)

        performance_graph = get_agent_graph("performance_optimization")
        final_state = performance_graph.invoke({
            "repo_owner": org_login,
            "repo_name": repo_name,
            "workflow_file": workflow_file,
            "workflow_yaml": workflow_yaml,
            "bottlenecks": bottlenecks,
            "parallelization_candidates": parallelization_candidates,
            "job_durations": job_durations,
            "needs_edges": [list(e) for e in needs_edges],
            "agent_trace": [],
        })

        now = datetime.now(timezone.utc)
        recommendation_ids = []
        for rec in final_state.get("recommendations", []):
            rec_id = uuid.uuid4()
            recommendation_ids.append(rec_id)
            session.execute(
                text(
                    """
                    INSERT INTO optimization_recommendations
                        (id, org_login, repo_name, workflow_file, graph_id, recommendation_type,
                         description, proposed_yaml_diff, estimated_time_savings_seconds,
                         confidence_score, status, agent_trace, created_at, updated_at)
                    VALUES
                        (:id, :org, :repo, :wf, :graph_id, :rtype, :description, :diff,
                         :savings, :confidence, 'proposed', :trace, :now, :now)
                    """
                ),
                {
                    "id": str(rec_id),
                    "org": org_login,
                    "repo": repo_name,
                    "wf": workflow_file,
                    "graph_id": str(graph_id),
                    "rtype": rec.get("type", "reorder"),
                    "description": rec.get("description", ""),
                    "diff": final_state.get("draft_future_yaml"),
                    "savings": rec.get("estimated_time_savings_seconds", 0),
                    "confidence": rec.get("confidence_score", 0),
                    "trace": final_state.get("agent_trace", []),
                    "now": now,
                },
            )

        if recommendation_ids:
            session.execute(
                text(
                    """
                    INSERT INTO simulation_runs
                        (id, recommendation_id, baseline_critical_path_seconds,
                         simulated_critical_path_seconds, delta_seconds, computed_at)
                    VALUES
                        (:id, :rec_id, :baseline, :simulated, :delta, :now)
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "rec_id": str(recommendation_ids[0]),
                    "baseline": final_state.get("baseline_critical_path_seconds", 0),
                    "simulated": final_state.get("simulated_critical_path_seconds", 0),
                    "delta": final_state.get("baseline_critical_path_seconds", 0) - final_state.get("simulated_critical_path_seconds", 0),
                    "now": now,
                },
            )

        session.commit()
        return {
            "status": "completed",
            "org_login": org_login,
            "repo_name": repo_name,
            "recommendations": len(recommendation_ids),
        }

    except Exception as exc:
        logger.exception("Optimization analysis failed for %s/%s %s: %s", org_login, repo_name, workflow_file, exc)
        raise self.retry(exc=exc)
    finally:
        session.close()
        if github:
            github.close()
