import json

import boto3

from app.core.config import settings
from app.services.bedrock_client import _bedrock_boto3_kwargs, _apply_bedrock_api_key

EMBED_MODEL_ID = "amazon.titan-embed-text-v2:0"
EMBED_DIM = 1024

def embed_text(text: str) -> list[float]:
    client = boto3.client(
        "bedrock-runtime",
        region_name=settings.AWS_REGION,
        **_bedrock_boto3_kwargs(),
    )
    _apply_bedrock_api_key(client)
    body = json.dumps({"inputText": text[:8000], "dimensions": EMBED_DIM, "normalize": True})
    resp = client.invoke_model(modelId=EMBED_MODEL_ID, body=body)
    payload = json.loads(resp["body"].read())
    return payload["embedding"]

def to_pgvector(embedding: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"
