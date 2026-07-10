# stagecraft-worker

The analysis engine of [StageCraft](https://github.com/StagecraftOps). Everything AI-shaped happens here: the LangGraph agent chains that root-cause failures, remediate vulnerabilities, review PRs, check governance/compliance, and optimize workflows — all calling AWS Bedrock.

**Stack**: Celery, LangGraph, boto3 (Bedrock/SQS), sync SQLAlchemy, Redis

## Two processes, one repo

| Process | Entry point | Job |
|---|---|---|
| **Celery worker** | `celery -A app.core.celery_app worker` | Executes analysis tasks from the Redis broker |
| **SQS consumer** | `python -m app.sqs_consumer` | Long-polls the `stagecraft-webhooks` SQS queue and dispatches each message to the right Celery task — the bridge between GitHub events and analysis |

The Helm chart deploys these as two separate Deployments sharing one ServiceAccount.

## What runs inside

- `app/tasks/` — one Celery task module per feature: `remediation`, `vulnerability`, `vulnerability_remediation`, `pr_review`, `governance`, `optimization`, `standardization`, `drift_detection`, `dependency_graph`, `knowledge_graph`, `job_timing`, `agent_report`
- `app/agents/` — the LangGraph graphs those tasks drive: the failure-remediation chain (classify → root cause → draft fix → security review → PR text; `graph.py`/`nodes.py`), plus dedicated graphs for compliance, governance, peer review, and performance, and the synchronous **Investigator** agent behind Pipeline Chat (`investigator.py`)
- Agents can call GitHub through [stagecraft-mcp](https://github.com/StagecraftOps/stagecraft-mcp) (`USE_MCP_TOOLS=true`, in-cluster over SSE) instead of direct API calls
- Results land in Postgres and are announced to the API via the internal API (`INTERNAL_API_KEY`) and Redis pub/sub, which the API relays to dashboards over WebSocket

## What it needs

| Dependency | Why |
|---|---|
| Redis | Celery broker/backend + pub/sub to the API |
| Postgres | Reads/writes the same DB as the API (sync driver) |
| SQS queue | Event intake (`SQS_QUEUE_URL`, `AWS_REGION`) |
| AWS Bedrock | All model calls (`BEDROCK_MODEL_ID`; auth via IRSA, `BEDROCK_API_KEY`, or `BEDROCK_CROSS_ACCOUNT_ROLE_ARN`) |
| GitHub App creds | Acting on repos (`GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`; `GITHUB_TOKEN` fallback for dev) |
| Shared secrets | `TOKEN_ENCRYPTION_KEY`, `SECRET_KEY`, `INTERNAL_API_KEY` — must match the API's values |

Full list with defaults: `app/core/config.py`. Optional: SES email notifications (`SES_ENABLED`), Bedrock knowledge base + guardrails, Neo4j dual-write.

## Run locally

```bash
cp .env.example .env   # AWS + GitHub creds required for real analysis
docker compose up --build   # starts worker + consumer (+ redis/postgres deps)
```

Tests: `pytest tests/`

## Related repos

| Repo | Purpose |
|------|---------|
| [stagecraft-webhook](https://github.com/StagecraftOps/stagecraft-webhook) | Fills the SQS queue this service drains |
| [stagecraft-api](https://github.com/StagecraftOps/stagecraft-api) | Owns the DB schema; receives this service's results |
| [stagecraft-mcp](https://github.com/StagecraftOps/stagecraft-mcp) | GitHub tools the agents call |
| [stagecraft-helm](https://github.com/StagecraftOps/stagecraft-helm) | Deploys both processes to EKS |
