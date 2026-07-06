import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import text

from app.analysis.critical_path import compute_critical_path
from app.analysis.workflow_parser import parse_workflow
from app.core.celery_app import app
from app.services.github_client import GitHubRemediationClient
from app.tasks.remediation import SyncSessionLocal, _get_github_token_for_org

logger = logging.getLogger(__name__)

def _parse_gh_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

def _duration_seconds(started: datetime | None, completed: datetime | None) -> int | None:
    if not started or not completed:
        return None
    return max(0, int((completed - started).total_seconds()))

def _match_job_id(gh_job_name: str, parsed_job_ids: list[str]) -> str | None:
    if gh_job_name in parsed_job_ids:
        return gh_job_name
    for job_id in parsed_job_ids:
        if gh_job_name.startswith(f"{job_id} ("):
            return job_id
    return None

@app.task(bind=True, max_retries=2, default_retry_delay=30)
def sync_job_timings_task(self, message: dict) -> dict:
    workflow_run_id = uuid.UUID(message["workflow_run_id"])
    repo_owner: str = message["repo_owner"]
    repo_name: str = message["repo_name"]
    run_id: int = message["run_id"]
    head_sha: str = message.get("head_sha", "")
    workflow_file: str = message.get("workflow_file", "")

    session = SyncSessionLocal()
    github: GitHubRemediationClient | None = None
    try:
        github_token = _get_github_token_for_org(session, repo_owner)
        github = GitHubRemediationClient(github_token)

        gh_jobs = github.get_run_jobs(repo_owner, repo_name, run_id)
        if not gh_jobs:
            return {"status": "no_jobs", "workflow_run_id": str(workflow_run_id)}

        now = datetime.now(timezone.utc)
        job_id_to_db_id: dict[str, uuid.UUID] = {}
        job_durations: dict[str, int] = {}

        for gh_job in gh_jobs:
            started = _parse_gh_timestamp(gh_job.get("started_at"))
            completed = _parse_gh_timestamp(gh_job.get("completed_at"))
            duration = _duration_seconds(started, completed)

            row = session.execute(
                text(
                    """
                    INSERT INTO job_runs
                        (id, workflow_run_id, github_job_id, job_name, status, conclusion,
                         started_at, completed_at, duration_seconds, runner_name,
                         runner_labels, runner_group_name, created_at)
                    VALUES
                        (:id, :workflow_run_id, :github_job_id, :job_name, :status, :conclusion,
                         :started_at, :completed_at, :duration_seconds, :runner_name,
                         :runner_labels, :runner_group_name, :created_at)
                    ON CONFLICT (github_job_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        conclusion = EXCLUDED.conclusion,
                        started_at = EXCLUDED.started_at,
                        completed_at = EXCLUDED.completed_at,
                        duration_seconds = EXCLUDED.duration_seconds,
                        runner_name = EXCLUDED.runner_name,
                        runner_labels = EXCLUDED.runner_labels,
                        runner_group_name = EXCLUDED.runner_group_name
                    RETURNING id
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "workflow_run_id": str(workflow_run_id),
                    "github_job_id": gh_job["id"],
                    "job_name": gh_job.get("name", ""),
                    "status": gh_job.get("status", "completed"),
                    "conclusion": gh_job.get("conclusion"),
                    "started_at": started,
                    "completed_at": completed,
                    "duration_seconds": duration,
                    "runner_name": gh_job.get("runner_name"),
                    "runner_labels": gh_job.get("labels") or None,
                    "runner_group_name": gh_job.get("runner_group_name"),
                    "created_at": now,
                },
            ).fetchone()
            job_db_id = uuid.UUID(str(row[0]))

            for step in gh_job.get("steps", []) or []:
                step_started = _parse_gh_timestamp(step.get("started_at"))
                step_completed = _parse_gh_timestamp(step.get("completed_at"))
                session.execute(
                    text(
                        """
                        INSERT INTO job_steps
                            (job_run_id, step_number, step_name, status, conclusion,
                             started_at, completed_at, duration_seconds)
                        VALUES
                            (:job_run_id, :step_number, :step_name, :status, :conclusion,
                             :started_at, :completed_at, :duration_seconds)
                        """
                    ),
                    {
                        "job_run_id": str(job_db_id),
                        "step_number": step.get("number", 0),
                        "step_name": step.get("name", ""),
                        "status": step.get("status", "completed"),
                        "conclusion": step.get("conclusion"),
                        "started_at": step_started,
                        "completed_at": step_completed,
                        "duration_seconds": _duration_seconds(step_started, step_completed),
                    },
                )

            gh_name = gh_job.get("name", "")
            job_id_to_db_id[gh_name] = job_db_id
            if duration is not None:
                job_durations[gh_name] = duration

        session.commit()

        try:
            workflow_yaml = github.get_workflow_yaml(repo_owner, repo_name, workflow_file, head_sha)
            _, edges = parse_workflow(workflow_file, workflow_yaml)
            parsed_job_ids = list({
                e["source_key"].split("::")[-1] for e in edges if e["edge_type"] == "needs"
            } | {
                e["target_key"].split("::")[-1] for e in edges if e["edge_type"] == "needs"
            })

            name_to_parsed: dict[str, str] = {}
            for gh_name in job_durations:
                matched = _match_job_id(gh_name, parsed_job_ids)
                if matched:
                    name_to_parsed[gh_name] = matched

            cp_jobs = [{"job_id": gh_name, "duration_seconds": dur} for gh_name, dur in job_durations.items()]
            needs_edges = [
                (e["source_key"].split("::")[-1], e["target_key"].split("::")[-1])
                for e in edges
                if e["edge_type"] == "needs"
            ]

            parsed_to_gh = {v: k for k, v in name_to_parsed.items()}
            translated_edges = [
                (parsed_to_gh.get(s, s), parsed_to_gh.get(t, t)) for s, t in needs_edges
            ]

            result = compute_critical_path(cp_jobs, translated_edges)
            if result["critical_path_job_ids"]:
                critical_path_db_ids = [
                    str(job_id_to_db_id[name])
                    for name in result["critical_path_job_ids"]
                    if name in job_id_to_db_id
                ]
                longest_db_id = job_id_to_db_id.get(result["longest_job_id"]) if result["longest_job_id"] else None

                session.execute(
                    text(
                        """
                        INSERT INTO critical_path_results
                            (id, workflow_run_id, total_duration_seconds, critical_path_job_ids,
                             longest_job_id, computed_at)
                        VALUES
                            (:id, :workflow_run_id, :total_duration_seconds, CAST(:critical_path_job_ids AS uuid[]),
                             :longest_job_id, :computed_at)
                        ON CONFLICT (workflow_run_id) DO UPDATE SET
                            total_duration_seconds = EXCLUDED.total_duration_seconds,
                            critical_path_job_ids = EXCLUDED.critical_path_job_ids,
                            longest_job_id = EXCLUDED.longest_job_id,
                            computed_at = EXCLUDED.computed_at
                        """
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "workflow_run_id": str(workflow_run_id),
                        "total_duration_seconds": result["total_duration_seconds"],
                        "critical_path_job_ids": critical_path_db_ids,
                        "longest_job_id": str(longest_db_id) if longest_db_id else None,
                        "computed_at": now,
                    },
                )
                session.commit()
        except Exception as cp_exc:
            logger.warning("Critical path computation skipped for run %s: %s", run_id, cp_exc)

        return {"status": "synced", "workflow_run_id": str(workflow_run_id), "jobs": len(gh_jobs)}

    except Exception as exc:
        logger.exception("Job timing sync failed for run %s: %s", run_id, exc)
        raise self.retry(exc=exc)
    finally:
        session.close()
        if github:
            github.close()
