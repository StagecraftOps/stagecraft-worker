from pydantic_settings import BaseSettings, SettingsConfigDict

INSECURE_DEFAULT_SECRET = "dev-insecure-secret-change-me"

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    AWS_REGION: str = "us-east-1"
    SQS_QUEUE_URL: str = "https://sqs.us-east-1.amazonaws.com/123456789/stagecraft-webhooks"
    BEDROCK_MODEL_ID: str = "anthropic.claude-sonnet-4-6"

    # Cross-account Bedrock access (Bedrock account).
    # When set, the worker assumes this role before every Bedrock call.
    # Leave empty to call Bedrock directly with the pod's IRSA role (same account).
    BEDROCK_CROSS_ACCOUNT_ROLE_ARN: str = ""

    # Long-lived Bedrock API key (ABSK… format).
    # When set, overrides IAM/role auth — injected as a Bearer token on every Bedrock call.
    BEDROCK_API_KEY: str = ""

    GITHUB_TOKEN: str = ""

    GITHUB_APP_ID: str = ""
    GITHUB_APP_PRIVATE_KEY: str = ""

    # MCP enrichment is optional. The worker already fetches the workflow and
    # failure logs, so remediation must work if this separate service is down.
    USE_MCP_TOOLS: bool = False
    MCP_GITHUB_URL: str = "http://stagecraft-mcp.stagecraft.svc.cluster.local:8010/sse"
    MCP_TOOL_TIMEOUT_SECONDS: float = 15.0

    DATABASE_URL: str = "postgresql://stagecraft:password@postgres:5432/stagecraft"
    REDIS_URL: str = "redis://redis:6379/0"

    SECRET_KEY: str = INSECURE_DEFAULT_SECRET
    TOKEN_ENCRYPTION_KEY: str = ""

    USE_MULTI_AGENT: bool = True

    # Shared secret checked on POST /internal/investigate (health.py) — the
    # Investigator Agent's entry point, called synchronously by stagecraft-api's
    # chat endpoint. Same key, same purpose as stagecraft-api's own
    # INTERNAL_API_KEY (gates its /internal/remediations/search route).
    INTERNAL_API_KEY: str = ""

    # "AI suggested a fix" email notification (SES). Sent to the org
    # owner's email after a remediation reaches status=analyzed. Best-effort
    # — a missing/unverified SES identity must never fail the remediation
    # itself. FRONTEND_URL builds the link to view the fix in the dashboard.
    SES_ENABLED: bool = False
    SES_FROM_EMAIL: str = ""
    FRONTEND_URL: str = "http://localhost:3000"

    # Bedrock Knowledge Base — replaces the pgvector log_embeddings pipeline.
    # Worker writes remediation docs to KB_S3_BUCKET; Bedrock ingests from S3.
    # Leave empty to fall back to the legacy pgvector path.
    BEDROCK_KB_ID: str = ""
    BEDROCK_KB_S3_BUCKET: str = ""

    # Bedrock Guardrail — applied to all converse() / InvokeModel calls.
    # Leave empty to skip guardrail (dev default until first terraform apply).
    BEDROCK_GUARDRAIL_ID: str = ""
    BEDROCK_GUARDRAIL_VERSION: str = ""

    # Neo4j — dependency/knowledge graph storage + GraphRAG retrieval.
    # GRAPH_DUAL_WRITE_NEO4J is additive only: the Postgres graph_nodes/
    # graph_edges write path never gets disabled by this, Neo4j is purely a
    # second write target while it's being verified. GRAPH_BACKEND controls
    # which store optimization.py's dependency-edge lookups read from —
    # leave both at their defaults (off/postgres) until dual-write is
    # confirmed correct.
    GRAPH_DUAL_WRITE_NEO4J: bool = False
    GRAPH_BACKEND: str = "postgres"
    NEO4J_URI: str = "bolt://stagecraft-neo4j.stagecraft.svc.cluster.local:7687"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str = ""


settings = Settings()
