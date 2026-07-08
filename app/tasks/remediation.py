import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

from cryptography.fernet import InvalidToken
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
import yaml

from app.core.celery_app import app
from app.core.config import settings
from app.core.security import decrypt_token
from app.services.bedrock_client import BedrockRemediationClient
from app.services.github_app import (
    get_installation_id_for_org,
    get_installation_token,
    github_app_configured,
)
from app.tasks.agent_report import record_agent_run
from app.services.github_client import GitHubRemediationClient

logger = logging.getLogger(__name__)

_sync_engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)
SyncSessionLocal = sessionmaker(bind=_sync_engine, autocommit=False, autoflush=False)

REDIS_EVENTS_CHANNEL = "stagecraft:events"

def _load_app_context(session: Session, org_login: str, repo_name: str) -> dict | None:
    row = session.execute(
        text(
            """
            SELECT risk_tier, regulatory_scope, language, framework, notes
            FROM application_contexts
            WHERE org_login = :org AND repo_name = :repo
            """
        ),
        {"org": org_login, "repo": repo_name},
    ).fetchone()
    if not row:
        return None
    return {
        "risk_tier": row[0],
        "regulatory_scope": row[1] or [],
        "language": row[2],
        "framework": row[3],
        "notes": row[4],
    }

def _compress_logs(text: str, max_lines: int = 200) -> str:
    lines = text.splitlines()
    deduped: list[str] = []
    prev = None
    run = 0
    for line in lines:
        if line == prev:
            run += 1
        else:
            if run > 1:
                deduped.append(f"  ... (repeated {run} times)")
            deduped.append(line)
            prev = line
            run = 1
    if run > 1:
        deduped.append(f"  ... (repeated {run} times)")
    if len(deduped) > max_lines:
        kept = max_lines // 2
        deduped = deduped[:kept] + [f"  ... ({len(deduped) - max_lines} lines omitted) ..."] + deduped[-kept:]
    return "\n".join(deduped)

def _strip_code_fences(text: str) -> str:
    value = (text or "").strip()
    if value.startswith("```"):
        value = "\n".join(
            line for line in value.splitlines() if not line.strip().startswith("```")
        ).strip()
    return value

_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF"
    "\U00002B00-\U00002BFF"
    "\U0000FE0F"
    "]+"
)

def _strip_emojis(text: str) -> str:
    return _EMOJI_PATTERN.sub("", text)

_INLINE_COMMENT = re.compile(r"(?<!['\"])\s#[^\n]*$")

def _strip_hallucinated_comments(original: str, candidate: str) -> str:
    original_comment_lines = {
        line.strip() for line in original.splitlines() if line.strip().startswith("#")
    }
    original_text = original

    kept = []
    for line in candidate.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            if stripped not in original_comment_lines:
                continue
            kept.append(line)
            continue

        if "#" in line and line not in original_text.splitlines():
            quote_parity_ok = line.count('"') % 2 == 0 and line.count("'") % 2 == 0
            match = _INLINE_COMMENT.search(line)
            if quote_parity_ok and match and match.group(0).strip() not in original_text:
                line = line[: match.start()]
        kept.append(line)
    return "\n".join(kept)

def _normalize_suggested_yaml(original: str, candidate: str | None) -> tuple[bool, str]:
    normalized = _strip_code_fences(candidate or "")
    if not normalized:
        return False, "empty output"

    normalized = _strip_emojis(normalized)
    normalized = _strip_hallucinated_comments(original, normalized)

    lines = []
    spacing_pattern = re.compile(r"^([ \t]*[a-zA-Z0-9_-]+):(?!\s)(.+)$")
    for line in normalized.splitlines():
        m = spacing_pattern.match(line)
        if m:
            lines.append(f"{m.group(1)}: {m.group(2)}")
        else:
            lines.append(line)
    normalized = "\n".join(lines)

    try:
        parsed = yaml.safe_load(normalized)
    except yaml.YAMLError:
        return False, "invalid YAML syntax"
    if not isinstance(parsed, dict) or "jobs" not in parsed:
        return False, "not a GitHub Actions workflow (no jobs:)"
    if normalized.strip() == original.strip():
        return False, "no change from original"
    return True, normalized

def _recover_suggested_yaml(
    workflow_yaml: str,
    root_cause: str,
    failure_category: str | None,
    logs: str,
    workflow_name: str,
    repo_full_name: str,
) -> str | None:
    bedrock = BedrockRemediationClient()

    candidate = bedrock.generate_yaml_fix(
        workflow_yaml=workflow_yaml,
        root_cause=root_cause,
        failure_category=failure_category or "UNKNOWN",
        logs=logs,
    )
    logger.warning("[RAW BEDROCK YAML] direct fallback candidate (first 800 chars): %r", (candidate or "")[:800])
    ok, normalized = _normalize_suggested_yaml(workflow_yaml, candidate)
    if ok:
        return normalized
    logger.warning("Direct YAML fallback produced an invalid fix (%s)", normalized)

    analysis = bedrock.analyze_failure(
        workflow_yaml, logs, workflow_name, repo_full_name
    )
    ok, normalized = _normalize_suggested_yaml(
        workflow_yaml, analysis.get("fixed_yaml")
    )
    if ok:
        return normalized
    logger.warning("Single-agent Bedrock fallback produced an invalid fix (%s)", normalized)
    return None

def _heuristic_yaml_fix(
    workflow_yaml: str,
    root_cause: str,
    failure_category: str | None,
) -> str | None:
    root_lower = (root_cause or "").lower()
    if "python" not in root_lower and "version" not in root_lower:
        return None

    replacements = [
        ('python-version: "99"', 'python-version: "3.12"'),
        ("python-version: '99'", 'python-version: "3.12"'),
        ("python-version: 99", 'python-version: "3.12"'),
    ]

    fixed = workflow_yaml
    changed = False
    for old, new in replacements:
        if old in fixed:
            fixed = fixed.replace(old, new)
            changed = True

    if not changed:
        return None

    ok, normalized = _normalize_suggested_yaml(workflow_yaml, fixed)
    if ok:
        logger.info("_heuristic_yaml_fix applied deterministic python-version fix")
    return normalized if ok else None

def _make_redis_sync():
    from urllib.parse import urlparse, urlencode, parse_qs, urlunparse
    import redis as redis_sync
    from redis.connection import ConnectionPool, SSLConnection

    url = settings.REDIS_URL
    parsed = urlparse(url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    qs.pop("ssl_cert_reqs", None)
    clean_url = urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))

    if clean_url.startswith("rediss://"):
        pool = ConnectionPool.from_url(
            clean_url, connection_class=SSLConnection, ssl_cert_reqs="none"
        )
    else:
        pool = ConnectionPool.from_url(clean_url)
    return redis_sync.Redis(connection_pool=pool)

def _publish_event(event_type: str, data: dict) -> None:
    try:
        r = _make_redis_sync()
        r.publish(REDIS_EVENTS_CHANNEL, json.dumps({"type": event_type, "data": data}))
        r.close()
    except Exception as exc:
        logger.warning("Failed to publish %s event to Redis: %s", event_type, exc)

def enqueue_knowledge_graph_rebuild(org_login: str) -> None:
    try:
        from app.tasks.knowledge_graph import build_knowledge_graph_task
        build_knowledge_graph_task.delay({"org_login": org_login})
    except Exception as exc:
        logger.warning("Failed to enqueue knowledge-graph rebuild for %s: %s", org_login, exc)

def _get_github_token_for_org(session: Session, org_login: str) -> str:

    if github_app_configured():
        row = session.execute(
            text("SELECT installation_id FROM organizations WHERE login = :login LIMIT 1"),
            {"login": org_login},
        ).fetchone()
        installation_id = row[0] if row else None

        if not installation_id:
            installation_id = asyncio.run(get_installation_id_for_org(org_login))

        if installation_id:
            return asyncio.run(get_installation_token(installation_id))

    row = session.execute(
        text(
            """
            SELECT u.access_token_encrypted
            FROM organizations o
            JOIN users u ON u.id = o.owner_id
            WHERE o.login = :login
            LIMIT 1
            """
        ),
        {"login": org_login},
    ).fetchone()

    if row:
        try:
            return decrypt_token(row[0])
        except InvalidToken as exc:
            raise RuntimeError(
                f"Token decryption failed for org '{org_login}'. "
                "Ensure SECRET_KEY / TOKEN_ENCRYPTION_KEY is identical across "
                "api-service and remediation-worker."
            ) from exc

    if settings.GITHUB_TOKEN:
        return settings.GITHUB_TOKEN

    raise RuntimeError(f"No GitHub token available for org '{org_login}'")

def _get_owner_email_for_org(session: Session, org_login: str) -> str | None:
    row = session.execute(
        text(
            """
            SELECT u.email
            FROM organizations o
            JOIN users u ON u.id = o.owner_id
            WHERE o.login = :login
            LIMIT 1
            """
        ),
        {"login": org_login},
    ).fetchone()
    return row[0] if row else None

def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

def _resolve_application_id(session: Session, org_login: str, repo_name: str | None) -> str | None:
    """Map a repo to its owning application (if assigned), so new rows are
    correctly attributed for per-application isolation."""
    if not repo_name:
        return None
    row = session.execute(
        text("SELECT application_id FROM application_repos WHERE org_login = :org AND repo_name = :repo"),
        {"org": org_login, "repo": repo_name},
    ).fetchone()
    return str(row[0]) if row and row[0] else None

def _upsert_workflow_run(session: Session, message: dict) -> uuid.UUID:
    run_id = message["run_id"]
    status_value = message.get("status") or "queued"
    conclusion = message.get("conclusion")
    started_at = _parse_timestamp(message.get("started_at"))
    completed_at = _parse_timestamp(message.get("completed_at"))
    html_url = message.get("html_url") or (
        f"https://github.com/{message['repo_owner']}/{message['repo_name']}"
        f"/actions/runs/{run_id}"
    )
    now = datetime.now(timezone.utc)

    row = session.execute(
        text(
            """
            INSERT INTO workflow_runs (
                id, github_run_id, github_workflow_id, org_login, repo_name, application_id,
                workflow_name, workflow_file, branch, head_sha,
                status, conclusion, started_at, completed_at, html_url,
                created_at, updated_at
            ) VALUES (
                :id, :run_id, :workflow_id, :org_login, :repo_name, :application_id,
                :workflow_name, :workflow_file, :branch, :head_sha,
                :status, :conclusion, :started_at, :completed_at, :html_url,
                :created_at, :updated_at
            )
            ON CONFLICT (github_run_id) DO UPDATE SET
                application_id = COALESCE(EXCLUDED.application_id, workflow_runs.application_id),
                -- Only advance status forward: queued < in_progress < completed.
                -- A late out-of-order SQS delivery must NEVER regress a
                -- completed run back to queued or in_progress.
                status = CASE
                    WHEN workflow_runs.status = 'completed' THEN workflow_runs.status
                    WHEN workflow_runs.status = 'in_progress' AND EXCLUDED.status = 'queued'
                        THEN workflow_runs.status
                    ELSE EXCLUDED.status
                END,
                -- Only update conclusion when we are actually completing the run.
                conclusion = CASE
                    WHEN EXCLUDED.status = 'completed' THEN EXCLUDED.conclusion
                    ELSE workflow_runs.conclusion
                END,
                started_at = COALESCE(EXCLUDED.started_at, workflow_runs.started_at),
                -- Only set completed_at when the run is actually completing.
                completed_at = CASE
                    WHEN EXCLUDED.status = 'completed'
                        THEN COALESCE(EXCLUDED.completed_at, workflow_runs.completed_at)
                    ELSE workflow_runs.completed_at
                END,
                html_url = EXCLUDED.html_url,
                updated_at = EXCLUDED.updated_at
            RETURNING id
            """
        ),
        {
            "id": str(uuid.uuid4()),
            "run_id": run_id,
            "workflow_id": message.get("workflow_id", 0),
            "org_login": message["repo_owner"],
            "repo_name": message["repo_name"],
            "application_id": _resolve_application_id(session, message["repo_owner"], message["repo_name"]),
            "workflow_name": message.get("workflow_name", ""),
            "workflow_file": message.get("workflow_file", ""),
            "branch": message.get("branch", ""),
            "head_sha": message.get("head_sha", ""),
            "status": status_value,
            "conclusion": conclusion,
            "started_at": started_at,
            "completed_at": completed_at,
            "html_url": html_url,
            "created_at": now,
            "updated_at": now,
        },
    ).fetchone()
    session.commit()
    return uuid.UUID(str(row[0]))

def _create_remediation_record(
    session: Session,
    workflow_run_id: uuid.UUID,
    message: dict,
    status: str,
) -> uuid.UUID:
    remediation_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    session.execute(
        text(
            """
            INSERT INTO remediations (
                id, workflow_run_id, org_login, repo_name, application_id, workflow_file,
                root_cause, fixed_yaml, suggested_yaml,
                bedrock_model, status, created_at, updated_at
            ) VALUES (
                :id, :workflow_run_id, :org_login, :repo_name, :application_id, :workflow_file,
                '', '', NULL,
                :bedrock_model, :status, :created_at, :updated_at
            )
            """
        ),
        {
            "id": str(remediation_id),
            "workflow_run_id": str(workflow_run_id),
            "org_login": message["repo_owner"],
            "repo_name": message["repo_name"],
            "application_id": _resolve_application_id(session, message["repo_owner"], message["repo_name"]),
            "workflow_file": message.get("workflow_file", ""),
            "bedrock_model": settings.BEDROCK_MODEL_ID,
            "status": status,
            "created_at": now,
            "updated_at": now,
        },
    )
    session.commit()
    return remediation_id

def _update_remediation(
    session: Session,
    remediation_id: uuid.UUID,
    status: str,
    root_cause: str = "",
    suggested_yaml: str | None = None,
    original_yaml: str | None = None,
    likely_code_level: bool = False,
    code_level_reasoning: str | None = None,
    error_message: str | None = None,
    failure_category: str | None = None,
    confidence_score: int | None = None,
    confidence_reasoning: str | None = None,
    security_risk_score: int | None = None,
    security_findings: list[str] | None = None,
    pr_title: str | None = None,
    pr_description: str | None = None,
    agent_trace: list[str] | None = None,
) -> None:
    session.execute(
        text(
            """
            UPDATE remediations SET
                status = :status,
                root_cause = :root_cause,
                suggested_yaml = :suggested_yaml,
                original_yaml = :original_yaml,
                likely_code_level = :likely_code_level,
                code_level_reasoning = :code_level_reasoning,
                error_message = :error_message,
                failure_category = :failure_category,
                confidence_score = :confidence_score,
                confidence_reasoning = :confidence_reasoning,
                security_risk_score = :security_risk_score,
                security_findings = :security_findings,
                pr_title = :pr_title,
                pr_description = :pr_description,
                agent_trace = :agent_trace,
                updated_at = :updated_at
            WHERE id = :id
            """
        ),
        {
            "id": str(remediation_id),
            "status": status,
            "root_cause": root_cause,
            "suggested_yaml": suggested_yaml,
            "original_yaml": original_yaml,
            "likely_code_level": likely_code_level,
            "code_level_reasoning": code_level_reasoning,
            "error_message": error_message,
            "failure_category": failure_category,
            "confidence_score": confidence_score,
            "confidence_reasoning": confidence_reasoning,
            "security_risk_score": security_risk_score,
            "security_findings": security_findings,
            "pr_title": pr_title,
            "pr_description": pr_description,
            "agent_trace": agent_trace,
            "updated_at": datetime.now(timezone.utc),
        },
    )
    session.commit()

def _ingest_embedding(
    session: Session,
    remediation_id: uuid.UUID,
    org_login: str,
    repo_name: str,
    workflow_file: str,
    failure_category: str | None,
    root_cause: str,
    suggested_yaml: str | None,
    logs_excerpt: str = "",
) -> None:
    chunk = (
        f"Repository: {org_login}/{repo_name}\n"
        f"Workflow: {workflow_file}\n"
        f"Failure category: {failure_category or 'UNKNOWN'}\n"
        f"Root cause: {root_cause}\n\n"
        f"Suggested fix (YAML):\n{(suggested_yaml or '')[:1500]}\n\n"
        f"Log excerpt:\n{logs_excerpt[:1500]}"
    )

    if settings.BEDROCK_KB_S3_BUCKET:
        import boto3
        s3 = boto3.client("s3", region_name=settings.AWS_REGION)
        key = f"remediations/{remediation_id}.txt"
        s3.put_object(
            Bucket=settings.BEDROCK_KB_S3_BUCKET,
            Key=key,
            Body=chunk.encode(),
            ContentType="text/plain",
        )
        logger.debug("KB doc written to s3://%s/%s", settings.BEDROCK_KB_S3_BUCKET, key)

        if settings.BEDROCK_KB_ID:
            try:
                import uuid as _uuid
                bedrock_agent = boto3.client("bedrock-agent", region_name=settings.AWS_REGION)

                ds_resp = bedrock_agent.list_data_sources(knowledgeBaseId=settings.BEDROCK_KB_ID)
                ds_id = ds_resp["dataSourceSummaries"][0]["dataSourceId"]
                bedrock_agent.start_ingestion_job(
                    knowledgeBaseId=settings.BEDROCK_KB_ID,
                    dataSourceId=ds_id,
                    clientToken=str(_uuid.uuid4()),
                )
                logger.info("Bedrock KB ingestion job started for KB %s", settings.BEDROCK_KB_ID)
            except Exception as exc:
                logger.warning("KB ingestion job failed to start (non-fatal): %s", exc)
        return

    from app.services.embeddings import embed_text, to_pgvector

    embedding = embed_text(chunk)
    meta = json.dumps({
        "org_login": org_login,
        "repo_name": repo_name,
        "workflow_file": workflow_file,
        "failure_category": failure_category or "UNKNOWN",
    })
    session.execute(
        text("DELETE FROM log_embeddings WHERE source_type = 'remediation' AND source_id = :sid"),
        {"sid": str(remediation_id)},
    )
    session.execute(
        text(
            """
            INSERT INTO log_embeddings
                (source_type, source_id, org_login, repo_name, failure_category,
                 chunk_text, embedding, metadata)
            VALUES
                ('remediation', :sid, :org, :repo, :cat,
                 :chunk, CAST(:emb AS vector), CAST(:meta AS jsonb))
            """
        ),
        {
            "sid": str(remediation_id),
            "org": org_login,
            "repo": repo_name,
            "cat": failure_category,
            "chunk": chunk,
            "emb": to_pgvector(embedding),
            "meta": meta,
        },
    )
    session.commit()

def _trigger_job_timing_sync(workflow_run_id: uuid.UUID, message: dict) -> None:
    try:
        from app.tasks.job_timing import sync_job_timings_task

        sync_job_timings_task.delay({
            "workflow_run_id": str(workflow_run_id),
            "repo_owner": message["repo_owner"],
            "repo_name": message["repo_name"],
            "run_id": message["run_id"],
            "head_sha": message.get("head_sha", ""),
            "workflow_file": message.get("workflow_file", ""),
        })
    except Exception as exc:
        logger.warning("Failed to enqueue job-timing sync for run %s: %s", message.get("run_id"), exc)

@app.task(bind=True, max_retries=3, default_retry_delay=30)
def upsert_workflow_run_task(self, message: dict) -> dict:
    session = SyncSessionLocal()
    try:
        run_uuid = _upsert_workflow_run(session, message)
        _publish_event("run_update", {
            "run_id": str(run_uuid),
            "github_run_id": message["run_id"],
            "status": message.get("status"),
            "conclusion": message.get("conclusion"),
            "org_login": message["repo_owner"],
            "repo_name": message["repo_name"],
        })
        if message.get("status") == "completed":
            _trigger_job_timing_sync(run_uuid, message)
        return {"status": "synced", "workflow_run_id": str(run_uuid)}
    except Exception as exc:
        logger.exception("Failed to sync workflow run %s: %s", message.get("run_id"), exc)
        raise self.retry(exc=exc)
    finally:
        session.close()

_STAGECRAFT_BOT_LOGIN = "stagecraftops[bot]"
_CODE_LEVEL_FIX_COOLDOWN_MINUTES = 15

def _recently_dispatched_code_level_fix(session: Session, org_login: str, repo_name: str) -> bool:
    """Second, independent guard against the brief-commit re-trigger loop --
    even if sender_login isn't populated for some event shape, don't dispatch
    another code-level fix for the same repo within the cooldown window."""
    row = session.execute(
        text(
            """
            SELECT 1 FROM agent_runs
            WHERE org_login = :org AND repo_name = :repo AND agent_name = 'failure_rca'
              AND outcome = 'dispatched' AND created_at > :since
            LIMIT 1
            """
        ),
        {
            "org": org_login, "repo": repo_name,
            "since": datetime.now(timezone.utc) - timedelta(minutes=_CODE_LEVEL_FIX_COOLDOWN_MINUTES),
        },
    ).fetchone()
    return row is not None

@app.task(bind=True, max_retries=3, default_retry_delay=60)
def process_failed_workflow(self, message: dict) -> dict:
    repo_owner: str = message["repo_owner"]
    repo_name: str = message["repo_name"]
    run_id: int = message["run_id"]
    workflow_file: str = message.get("workflow_file", "")
    head_sha: str = message.get("head_sha", "")
    workflow_name: str = message.get("workflow_name", "")
    branch: str = message.get("branch", "")
    sender_login: str = message.get("sender_login", "")

    logger.info("Analyzing failed workflow run %s for %s/%s", run_id, repo_owner, repo_name)

    if sender_login == _STAGECRAFT_BOT_LOGIN:
        # A failure caused by our own bot's commit (e.g. committing an updated
        # BRIEF.md, which is itself a push and re-triggers CI) isn't new
        # information -- it's the same still-unfixed bug re-announcing itself.
        # Without this guard, dispatching a fresh fix attempt here creates an
        # unbounded loop: brief commit -> CI re-runs -> fails -> dispatch ->
        # brief commit -> ... (this happened for real; see incident notes).
        logger.info(
            "Skipping analysis for run %s in %s/%s -- triggered by our own bot (%s), not a new failure",
            run_id, repo_owner, repo_name, sender_login,
        )
        return {"status": "skipped_own_bot"}

    session = SyncSessionLocal()
    github: GitHubRemediationClient | None = None
    workflow_run_id: uuid.UUID | None = None
    remediation_id: uuid.UUID | None = None

    try:
        workflow_run_id = _upsert_workflow_run(session, message)
        _publish_event("run_update", {
            "run_id": str(workflow_run_id),
            "status": "completed",
            "conclusion": "failure",
            "org_login": repo_owner,
            "repo_name": repo_name,
        })
        _trigger_job_timing_sync(workflow_run_id, message)

        remediation_id = _create_remediation_record(session, workflow_run_id, message, "analyzing")
        _publish_event("remediation_created", {"id": str(remediation_id), "status": "analyzing"})

        github_token = _get_github_token_for_org(session, repo_owner)
        github = GitHubRemediationClient(github_token)

        logger.info("Fetching workflow YAML: %s@%s", workflow_file, head_sha)
        workflow_yaml = github.get_workflow_yaml(repo_owner, repo_name, workflow_file, head_sha)

        logger.info("Fetching logs for run %s", run_id)
        logs = github.get_run_logs(repo_owner, repo_name, run_id)

        from app.agents.scrubber import scrub
        scrubbed_logs = scrub(logs)
        workflow_yaml = scrub(workflow_yaml)

        scrubbed_logs = _compress_logs(scrubbed_logs)

        if settings.USE_MULTI_AGENT:
            logger.info("Running multi-agent LangGraph pipeline for run %s", run_id)
            from app.agents.graph import remediation_graph

            fix_examples: list[str] = []
            try:
                rows = session.execute(
                    text(
                        """
                        SELECT fixed_yaml FROM fix_memories
                        WHERE org_login = :org AND repo_name = :repo
                        ORDER BY created_at DESC
                        LIMIT 5
                        """
                    ),
                    {"org": repo_owner, "repo": repo_name},
                ).fetchall()
                fix_examples = [r[0] for r in rows if r[0]]
            except Exception as fm_exc:
                logger.debug("fix_memories fetch skipped: %s", fm_exc)

            app_context = _load_app_context(session, repo_owner, repo_name)

            final_state = remediation_graph.invoke({
                "repo_owner": repo_owner,
                "repo_name": repo_name,
                "workflow_file": workflow_file,
                "workflow_yaml": workflow_yaml,
                "logs": scrubbed_logs,
                "head_sha": head_sha,
                "run_id": run_id,
                "github_token": github_token,
                "app_context": app_context,
                "agent_trace": [],
                "fix_examples": fix_examples,
            })
            if final_state.get("error"):
                raise RuntimeError(final_state["error"])
            root_cause = final_state.get("root_cause", "")
            suggested_yaml = final_state.get("suggested_yaml")
            pr_title = final_state.get("pr_title", "")
            pr_description = final_state.get("pr_description")
            failure_category = final_state.get("failure_category")
            confidence_score = final_state.get("confidence_score")
            confidence_reasoning = final_state.get("confidence_reasoning")
            security_risk_score = final_state.get("security_risk_score")
            security_findings = final_state.get("security_findings")
            agent_trace = final_state.get("agent_trace")
            likely_code_level = final_state.get("likely_code_level", False)
            code_level_reasoning = final_state.get("code_level_reasoning") or None
            logger.info(
                "Multi-agent trace: %s | pr_title: %s | security_risk: %s | confidence: %s",
                agent_trace,
                pr_title,
                security_risk_score,
                confidence_score,
            )
        else:
            logger.info("Invoking Bedrock (single-agent) for run %s", run_id)
            bedrock = BedrockRemediationClient()
            analysis = bedrock.analyze_failure(
                workflow_yaml, scrubbed_logs, workflow_name, f"{repo_owner}/{repo_name}"
            )
            root_cause = analysis.get("root_cause", "")
            suggested_yaml = analysis.get("fixed_yaml")
            pr_title = analysis.get("pr_title", "")
            pr_description = None
            failure_category = None
            confidence_score = None
            confidence_reasoning = None
            security_risk_score = None
            security_findings = None
            agent_trace = None
            likely_code_level = False
            code_level_reasoning = None

        if not suggested_yaml and root_cause and not likely_code_level:
            logger.warning(
                "Analysis found root cause but no valid YAML fix for run %s; trying direct Bedrock fallback",
                run_id,
            )
            suggested_yaml = _recover_suggested_yaml(
                workflow_yaml=workflow_yaml,
                root_cause=root_cause,
                failure_category=failure_category,
                logs=scrubbed_logs,
                workflow_name=workflow_name,
                repo_full_name=f"{repo_owner}/{repo_name}",
            )

        if not suggested_yaml and not likely_code_level:
            suggested_yaml = _heuristic_yaml_fix(
                workflow_yaml=workflow_yaml,
                root_cause=root_cause,
                failure_category=failure_category,
            )

        if not suggested_yaml:
            message = (
                "Root cause appears to be in the application's own code or repository content, "
                "not the pipeline configuration -- no automated YAML fix applies."
                if likely_code_level
                else "AI identified the root cause but could not produce a valid YAML fix."
            )
            _update_remediation(
                session,
                remediation_id,
                status="failed",
                root_cause=root_cause,
                suggested_yaml=None,
                likely_code_level=likely_code_level,
                code_level_reasoning=code_level_reasoning,
                error_message=message,
                failure_category=failure_category,
                confidence_score=0,
                confidence_reasoning="No valid YAML suggestion was produced.",
                security_risk_score=security_risk_score,
                security_findings=security_findings,
                pr_title=pr_title,
                pr_description=pr_description,
                agent_trace=agent_trace,
            )
            _publish_event("remediation_updated", {
                "id": str(remediation_id),
                "status": "failed",
                "root_cause": root_cause,
            })
            logger.warning(
                "Analysis completed without a valid YAML fix for run %s (remediation %s)",
                run_id,
                remediation_id,
            )
            record_agent_run(
                session,
                org_login=repo_owner,
                repo_name=repo_name,
                agent_name="failure_rca",
                outcome="needs_review",
                summary=(
                    f"Flagged as an application code/repo-content issue in {workflow_file} ({repo_name}), "
                    f"not a pipeline fix: {code_level_reasoning}"
                    if likely_code_level
                    else f"Root cause found for {workflow_file} in {repo_name}, but no valid YAML fix could be produced."
                ),
                gaps_found=1,
            )
            session.commit()
            if failure_category:
                enqueue_knowledge_graph_rebuild(repo_owner)
            if likely_code_level and not _recently_dispatched_code_level_fix(session, repo_owner, repo_name):
                run_code_level_fix_task.delay({
                    "org_login": repo_owner,
                    "repo_name": repo_name,
                    "workflow_name": workflow_name,
                    "workflow_file": workflow_file,
                    "root_cause": root_cause,
                    "code_level_reasoning": code_level_reasoning,
                    "failure_category": failure_category,
                    "logs": scrubbed_logs,
                    "branch": branch,
                })
            return {"status": "failed", "remediation_id": str(remediation_id)}

        _update_remediation(
            session,
            remediation_id,
            status="analyzed",
            root_cause=root_cause,
            suggested_yaml=suggested_yaml,
            original_yaml=workflow_yaml,
            failure_category=failure_category,
            confidence_score=confidence_score,
            confidence_reasoning=confidence_reasoning,
            security_risk_score=security_risk_score,
            security_findings=security_findings,
            pr_title=pr_title,
            pr_description=pr_description,
            agent_trace=agent_trace,
        )
        _publish_event("remediation_updated", {
            "id": str(remediation_id),
            "status": "analyzed",
            "root_cause": root_cause,
        })
        if failure_category:
            enqueue_knowledge_graph_rebuild(repo_owner)

        record_agent_run(
            session,
            org_login=repo_owner,
            repo_name=repo_name,
            agent_name="failure_rca",
            outcome="needs_review",
            summary=f"Proposed a fix for {workflow_file} in {repo_name}: {root_cause}",
            gaps_found=1,
            artifacts=[str(remediation_id)],
        )
        session.commit()

        try:
            _ingest_embedding(
                session, remediation_id, repo_owner, repo_name, workflow_file,
                failure_category, root_cause, suggested_yaml, scrubbed_logs,
            )
        except Exception as embed_exc:
            logger.warning("Embedding ingestion failed for remediation %s: %s", remediation_id, embed_exc)

        try:
            owner_email = _get_owner_email_for_org(session, repo_owner)
            if owner_email:
                from app.services.email import send_fix_notification
                send_fix_notification(
                    owner_email, repo_name, failure_category, root_cause, str(remediation_id),
                )
            else:
                logger.info(
                    "No email on file for %s's owner — skipping fix notification for remediation %s",
                    repo_owner, remediation_id,
                )
        except Exception as email_exc:
            logger.warning("Fix notification email failed for remediation %s: %s", remediation_id, email_exc)

        logger.info("Analysis completed for run %s (remediation %s)", run_id, remediation_id)
        return {"status": "analyzed", "remediation_id": str(remediation_id)}

    except Exception as exc:
        logger.exception("Analysis failed for run %s: %s", run_id, exc)

        if remediation_id:
            try:
                _update_remediation(
                    session, remediation_id, status="failed", error_message=str(exc)
                )
                _publish_event("remediation_updated", {
                    "id": str(remediation_id),
                    "status": "failed",
                })
            except Exception as update_exc:
                logger.error("Failed to mark remediation as failed: %s", update_exc)

        raise self.retry(exc=exc)

    finally:
        session.close()
        if github:
            github.close()

def _build_failure_brief(
    org_login: str, repo_name: str, workflow_name: str, workflow_file: str,
    root_cause: str, code_level_reasoning: str | None, failure_category: str | None,
    logs: str, app_context: dict | None,
) -> str:
    """Brief for claude-code-action when the Self-Healing RCA agent has
    already determined a failure's root cause is in the application's own
    source, not the pipeline YAML -- gives the agent a starting point (the
    RCA analysis + a log excerpt), then lets it explore the actual checked-
    out repo to find and fix the real code."""
    lines = [
        f"# StageCraft Failure Brief -- {org_login}/{repo_name}",
        "",
        f"## Failed workflow: {workflow_name} ({workflow_file})",
        "",
        "## Root cause (from automated analysis)",
        "",
        root_cause or "Not determined.",
        "",
    ]
    if code_level_reasoning:
        lines += ["## Why this is a code-level issue, not a pipeline config issue", "", code_level_reasoning, ""]
    if failure_category:
        lines += [f"Failure category: {failure_category}", ""]
    if app_context:
        lines += [
            "## Application Context",
            "",
            f"- Risk tier: {app_context.get('risk_tier') or 'unknown'}",
            f"- Regulatory scope: {', '.join(app_context.get('regulatory_scope') or []) or 'none'}",
            f"- Data classification: {app_context.get('data_classification') or 'unknown'}",
            "",
        ]
    if logs:
        lines += ["## Relevant log excerpt", "", "```", logs[-4000:], "```", ""]
    lines += [
        "## Instructions",
        "",
        "This is NOT a pipeline/workflow-config issue -- the fix is in the application's own "
        "source code. Explore the repository to find the actual root cause (the log excerpt above "
        "is a starting point, not the full picture), make the minimal correct fix, and open a PR.",
    ]
    return "\n".join(lines)

@app.task(bind=True, max_retries=1, default_retry_delay=30)
def run_code_level_fix_task(self, message: dict) -> dict:
    from app.tasks.vulnerability_remediation import _BRIEF_PATH, _REMEDIATION_AGENT_WORKFLOW_PATH

    org_login = message["org_login"]
    repo_name = message["repo_name"]

    session = SyncSessionLocal()
    github: GitHubRemediationClient | None = None
    try:
        github_token = _get_github_token_for_org(session, org_login)
        github = GitHubRemediationClient(github_token)
        default_branch = github.get_default_branch(org_login, repo_name)

        if not github.get_file_sha(org_login, repo_name, _REMEDIATION_AGENT_WORKFLOW_PATH, default_branch):
            record_agent_run(
                session, org_login=org_login, repo_name=repo_name, agent_name="failure_rca",
                outcome="needs_review",
                summary="Code-level root cause found, but the remediation agent isn't deployed to this "
                        "repo yet -- publish it from the Vulnerability Remediation page first.",
            )
            session.commit()
            return {"status": "not_deployed"}

        app_context = _load_app_context(session, org_login, repo_name)
        brief = _build_failure_brief(
            org_login, repo_name, message.get("workflow_name", ""), message.get("workflow_file", ""),
            message.get("root_cause", ""), message.get("code_level_reasoning"),
            message.get("failure_category"), message.get("logs", ""), app_context,
        )
        brief_sha = github.get_file_sha(org_login, repo_name, _BRIEF_PATH, default_branch)
        github.commit_fix(
            org_login, repo_name, default_branch, _BRIEF_PATH, brief,
            "chore: update StageCraft failure brief", brief_sha,
        )
        github.dispatch_workflow(
            org_login, repo_name, "stagecraft-remediation-agent.yml", default_branch,
            {"brief_path": _BRIEF_PATH},
        )

        record_agent_run(
            session, org_login=org_login, repo_name=repo_name, agent_name="failure_rca",
            outcome="dispatched",
            summary=f"Dispatched claude-code-action to fix a code-level failure in {repo_name}: "
                    f"{message.get('root_cause', '')[:200]}",
        )
        session.commit()
        return {"status": "dispatched"}
    except Exception as exc:
        logger.exception("run_code_level_fix_task failed for %s/%s: %s", org_login, repo_name, exc)
        raise self.retry(exc=exc)
    finally:
        session.close()
        if github:
            github.close()

@app.task(bind=True, max_retries=1, default_retry_delay=60)
def backfill_embeddings_task(self, limit: int = 500) -> dict:
    session = SyncSessionLocal()
    embedded = 0
    try:
        rows = session.execute(
            text(
                """
                SELECT r.id, r.org_login, r.repo_name, r.workflow_file,
                       r.failure_category, r.root_cause, r.suggested_yaml
                FROM remediations r
                WHERE r.root_cause <> ''
                  AND NOT EXISTS (
                      SELECT 1 FROM log_embeddings e
                      WHERE e.source_type = 'remediation' AND e.source_id = r.id
                  )
                ORDER BY r.created_at DESC
                LIMIT :lim
                """
            ),
            {"lim": limit},
        ).fetchall()

        for row in rows:
            try:
                _ingest_embedding(
                    session, row.id, row.org_login, row.repo_name, row.workflow_file,
                    row.failure_category, row.root_cause, row.suggested_yaml, "",
                )
                embedded += 1
            except Exception as exc:
                logger.warning("Backfill embedding failed for remediation %s: %s", row.id, exc)

        logger.info("Embedding backfill complete: %s remediations embedded", embedded)
        return {"status": "completed", "embedded": embedded}
    finally:
        session.close()

_BACKFILL_MAX_RUNS_PER_REPO = 200
_BACKFILL_LOW_RATE_LIMIT_THRESHOLD = 50

def _set_org_sync_status(session: Session, org_login: str, status_value: str) -> None:
    session.execute(
        text("UPDATE organizations SET sync_status = :status WHERE login = :login"),
        {"status": status_value, "login": org_login},
    )
    session.commit()

def _get_org_id(session: Session, org_login: str) -> str | None:
    row = session.execute(
        text("SELECT id FROM organizations WHERE login = :login"), {"login": org_login}
    ).fetchone()
    return str(row[0]) if row else None

def _get_active_repo_names(session: Session, org_id: str) -> set[str] | None:
    """None means no scope has been configured -- treat every repo as active
    (keeps existing orgs, which predate the Select Scope step, working as before)."""
    rows = session.execute(
        text("SELECT repo_name FROM org_repo_scope WHERE org_id = :org_id AND is_active = true"),
        {"org_id": org_id},
    ).fetchall()
    if not rows:
        return None
    return {r[0] for r in rows}

def _set_repo_progress(
    session: Session,
    org_id: str,
    repo_name: str,
    status_value: str,
    runs_synced: int = 0,
    error: str | None = None,
) -> None:
    import uuid as _uuid

    session.execute(
        text(
            """
            INSERT INTO org_sync_progress (id, org_id, repo_name, status, runs_synced, error, updated_at)
            VALUES (:id, :org_id, :repo_name, :status, :runs_synced, :error, now())
            ON CONFLICT (org_id, repo_name) DO UPDATE
              SET status = EXCLUDED.status,
                  runs_synced = EXCLUDED.runs_synced,
                  error = EXCLUDED.error,
                  updated_at = now()
            """
        ),
        {
            "id": str(_uuid.uuid4()),
            "org_id": org_id,
            "repo_name": repo_name,
            "status": status_value,
            "runs_synced": runs_synced,
            "error": error,
        },
    )
    session.commit()

@app.task(bind=True, max_retries=2, default_retry_delay=120)
def backfill_org_runs_task(self, org_login: str) -> dict:
    session = SyncSessionLocal()
    github: GitHubRemediationClient | None = None
    synced = 0
    failed_repos: list[str] = []

    try:
        _set_org_sync_status(session, org_login, "syncing")
        org_id = _get_org_id(session, org_login)
        github_token = _get_github_token_for_org(session, org_login)
        github = GitHubRemediationClient(github_token)

        repos = github.get_org_repos(org_login)
        active_repo_names = _get_active_repo_names(session, org_id) if org_id else None
        if active_repo_names is not None:
            repos = [r for r in repos if r["name"] in active_repo_names]

        for repo in repos:
            repo_name = repo["name"]
            page = 1
            repo_synced = 0
            if org_id:
                _set_repo_progress(session, org_id, repo_name, "syncing")

            try:
                while repo_synced < _BACKFILL_MAX_RUNS_PER_REPO:
                    runs, rate_limit_remaining = github.get_repo_runs(
                        org_login, repo_name, per_page=100, page=page
                    )
                    if not runs:
                        break

                    for run in runs:
                        msg = {
                            "repo_owner": org_login,
                            "repo_name": repo_name,
                            "run_id": run["id"],
                            "workflow_id": run.get("workflow_id"),
                            "workflow_name": run.get("name", ""),
                            "workflow_file": run.get("path", ""),
                            "branch": run.get("head_branch", ""),
                            "head_sha": run.get("head_sha", ""),
                            "status": run.get("status"),
                            "conclusion": run.get("conclusion"),
                            "started_at": run.get("run_started_at"),
                            "completed_at": run.get("updated_at")
                            if run.get("status") == "completed"
                            else None,
                            "html_url": run.get("html_url"),
                        }
                        _upsert_workflow_run(session, msg)
                        repo_synced += 1
                        synced += 1
                        if repo_synced >= _BACKFILL_MAX_RUNS_PER_REPO:
                            break

                    if len(runs) < 100:
                        break
                    page += 1

                    if (
                        rate_limit_remaining is not None
                        and rate_limit_remaining < _BACKFILL_LOW_RATE_LIMIT_THRESHOLD
                    ):
                        logger.warning("Rate limit low (%s), pausing backfill", rate_limit_remaining)
                        time.sleep(5)

                if org_id:
                    _set_repo_progress(session, org_id, repo_name, "completed", repo_synced)

            except Exception as repo_exc:
                logger.exception("Backfill failed for %s/%s: %s", org_login, repo_name, repo_exc)
                session.rollback()
                failed_repos.append(repo_name)
                if org_id:
                    _set_repo_progress(
                        session, org_id, repo_name, "failed", repo_synced, str(repo_exc)[:2000]
                    )
                continue

            time.sleep(0.5)

        final_status = "completed_with_errors" if failed_repos else "completed"
        _set_org_sync_status(session, org_login, final_status)
        logger.info(
            "Backfill %s for %s: %s runs, %s repos failed",
            final_status, org_login, synced, len(failed_repos),
        )
        return {
            "status": final_status,
            "org_login": org_login,
            "synced": synced,
            "failed_repos": failed_repos,
        }

    except Exception as exc:
        logger.exception("Backfill failed for %s: %s", org_login, exc)
        try:
            _set_org_sync_status(session, org_login, "failed")
        except Exception:
            pass
        raise self.retry(exc=exc)

    finally:
        session.close()
        if github:
            github.close()

@app.task(bind=True, max_retries=3, default_retry_delay=10)
def register_app_installation_task(self, message: dict) -> dict:
    import uuid as _uuid

    action = message.get("action")
    org_login = message.get("org_login")
    org_id = message.get("org_id")
    installation_id = message.get("installation_id")
    avatar_url = message.get("avatar_url")
    sender_id = message.get("sender_id")
    sender_login = message.get("sender_login")

    session = SyncSessionLocal()
    try:
        if action == "created":
            owner_id = None
            if sender_id is not None:
                user_row = session.execute(
                    text("SELECT id FROM users WHERE github_id = :github_id"),
                    {"github_id": sender_id},
                ).fetchone()
                owner_id = str(user_row[0]) if user_row else None

            if owner_id is None:
                logger.warning(
                    "Install sender %s (github_id=%s) has no matching StageCraft "
                    "user for org %s; org will be unclaimed until they log in.",
                    sender_login, sender_id, org_login,
                )

            session.execute(
                text(
                    """
                    INSERT INTO organizations
                      (id, github_org_id, login, avatar_url, webhook_secret,
                       installation_id, sync_status, owner_id,
                       installed_by_github_id, installed_by_login, created_at)
                    VALUES
                      (:id, :github_org_id, :login, :avatar_url, '',
                       :installation_id, 'pending', :owner_id,
                       :sender_id, :sender_login, now())
                    ON CONFLICT (login) DO UPDATE
                      SET installation_id = EXCLUDED.installation_id,
                          avatar_url = EXCLUDED.avatar_url,
                          owner_id = COALESCE(organizations.owner_id, EXCLUDED.owner_id),
                          installed_by_github_id = EXCLUDED.installed_by_github_id,
                          installed_by_login = EXCLUDED.installed_by_login
                    """
                ),
                {
                    "id": str(_uuid.uuid4()),
                    "github_org_id": org_id,
                    "login": org_login,
                    "avatar_url": avatar_url,
                    "installation_id": installation_id,
                    "owner_id": owner_id,
                    "sender_id": sender_id,
                    "sender_login": sender_login,
                },
            )
            session.commit()
            logger.info("Registered App installation %s for org %s", installation_id, org_login)
            if owner_id is not None:
                backfill_org_runs_task.delay(org_login)
            return {"status": "registered" if owner_id else "unclaimed", "org_login": org_login}

        elif action == "deleted":
            session.execute(
                text("DELETE FROM organizations WHERE login = :login"),
                {"login": org_login},
            )
            session.commit()
            logger.info("Removed org %s after App uninstall", org_login)
            return {"status": "removed", "org_login": org_login}

        return {"status": "ignored", "action": action}

    except Exception as exc:
        session.rollback()
        logger.exception("register_app_installation_task failed: %s", exc)
        raise self.retry(exc=exc)
    finally:
        session.close()
