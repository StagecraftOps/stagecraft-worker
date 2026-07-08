from pydantic_settings import BaseSettings, SettingsConfigDict

INSECURE_DEFAULT_SECRET = "dev-insecure-secret-change-me"

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    AWS_REGION: str = "us-east-1"
    SQS_QUEUE_URL: str = "https://sqs.us-east-1.amazonaws.com/123456789/stagecraft-webhooks"
    BEDROCK_MODEL_ID: str = "anthropic.claude-sonnet-4-6"

    BEDROCK_CROSS_ACCOUNT_ROLE_ARN: str = ""

    BEDROCK_API_KEY: str = ""

    GITHUB_TOKEN: str = ""
    COPILOT_PAT: str = ""

    GITHUB_APP_ID: str = ""
    GITHUB_APP_PRIVATE_KEY: str = ""

    USE_MCP_TOOLS: bool = False
    MCP_GITHUB_URL: str = "http://stagecraft-mcp.stagecraft.svc.cluster.local:8010/sse"
    MCP_TOOL_TIMEOUT_SECONDS: float = 15.0

    DATABASE_URL: str = "postgresql://stagecraft:password@postgres:5432/stagecraft"
    REDIS_URL: str = "redis://redis:6379/0"

    SECRET_KEY: str = INSECURE_DEFAULT_SECRET
    TOKEN_ENCRYPTION_KEY: str = ""

    USE_MULTI_AGENT: bool = True

    INTERNAL_API_KEY: str = ""

    SES_ENABLED: bool = False
    SES_FROM_EMAIL: str = ""
    FRONTEND_URL: str = "http://localhost:3000"

    BEDROCK_KB_ID: str = ""
    BEDROCK_KB_S3_BUCKET: str = ""

    BEDROCK_GUARDRAIL_ID: str = ""
    BEDROCK_GUARDRAIL_VERSION: str = ""

    GRAPH_DUAL_WRITE_NEO4J: bool = False
    GRAPH_BACKEND: str = "postgres"
    NEO4J_URI: str = "bolt://stagecraft-neo4j.stagecraft.svc.cluster.local:7687"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str = ""

settings = Settings()
