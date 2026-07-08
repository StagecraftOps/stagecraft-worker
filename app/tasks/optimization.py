import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import text

from app.agents.registry import get_agent_graph
from app.analysis.bottleneck_detector import detect_bottlenecks
from app.analysis.parallelization_advisor import find_parallelization_candidates
from app.core.celery_app import app
from app.core.config import settings
from app.services.github_client import GitHubRemediationClient
from app.services.neo4j_client import get_driver
from app.tasks.agent_report import record_agent_run
from app.tasks.remediation import SyncSessionLocal, _get_github_token_for_org, enqueue_knowledge_graph_rebuild

logger = logging.getLogger(__name__)

def _job_name_from_key(external_key: str) -> str:

    return external_key.rsplit("::", 1)[-1]

def _fetch_job_edges_neo4j(org_login: str, repo_name: str, workflow_file: str, rel: str) -> list[tuple[str, str]]:
    with get_driver().session() as neo_session:
        result = neo_session.run(
            f"""
            MATCH (src:GraphNode {{org_login: $org, repo_name: $repo, workflow_file: $wf}})
                  -[:{rel}]->
                  (tgt:GraphNode {{org_login: $org, repo_name: $repo, workflow_file: $wf}})
            RETURN src.external_key AS source_key, tgt.external_key AS target_key
            """,
            org=org_login, repo=repo_name, wf=workflow_file,
        )
        return [(_job_name_from_key(r["source_key"]), _job_name_from_key(r["target_key"])) for r in result]

def _has_dependency_graph_neo4j(org_login: str, repo_name: str, workflow_file: str) -> bool:
    with get_driver().session() as neo_session:
        record = neo_session.run(
            "MATCH (n:GraphNode:Workflow {org_login: $org, repo_name: $repo, workflow_file: $wf}) "
            "RETURN count(n) AS c",
            org=org_login, repo=repo_name, wf=workflow_file,
        ).single()
        return bool(record and record["c"] > 0)

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

        if settings.GRAPH_BACKEND == "neo4j":
            if not _has_dependency_graph_neo4j(org_login, repo_name, workflow_file):
                return {"status": "no_graph", "org_login": org_login, "repo_name": repo_name}
            needs_edges = _fetch_job_edges_neo4j(org_login, repo_name, workflow_file, "NEEDS")
            needs_output_edges = _fetch_job_edges_neo4j(org_login, repo_name, workflow_file, "NEEDS_OUTPUT")
        else:
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
                         description, original_yaml, proposed_yaml_diff, estimated_time_savings_seconds,
                         confidence_score, status, agent_trace, created_at, updated_at)
                    VALUES
                        (:id, :org, :repo, :wf, :graph_id, :rtype, :description, :original, :diff,
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
                    "original": workflow_yaml,
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

        record_agent_run(
            session,
            org_login=org_login,
            repo_name=repo_name,
            agent_name="performance_optimization",
            outcome="needs_review" if recommendation_ids else "success",
            summary=(
                f"{len(recommendation_ids)} optimization recommendation(s) for {workflow_file} in {repo_name}."
                if recommendation_ids else f"No optimization opportunities found for {workflow_file} in {repo_name}."
            ),
            gaps_found=len(recommendation_ids),
        )
        session.commit()

        if recommendation_ids:
            enqueue_knowledge_graph_rebuild(org_login)

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
