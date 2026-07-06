import json
from unittest.mock import MagicMock, patch
import pytest

import os
os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/x")
os.environ.setdefault("SECRET_KEY", "test-secret-only")

def _make_bedrock_response(content: str) -> dict:
    return {"output": {"message": {"content": [{"text": content}]}}}

@patch("boto3.client")
def test_valid_json_response_parsed(mock_boto):
    from app.services.bedrock_client import BedrockRemediationClient

    good_response = json.dumps({
        "root_cause": "Missing env var NODE_ENV",
        "fixed_yaml": "name: CI\non: push\njobs: {}",
        "pr_title": "fix: add NODE_ENV",
        "pr_description": "Adds missing NODE_ENV variable",
    })

    mock_client = MagicMock()
    mock_client.converse.return_value = _make_bedrock_response(good_response)
    mock_client.exceptions.ThrottlingException = type("ThrottlingException", (Exception,), {})
    mock_boto.return_value = mock_client

    client = BedrockRemediationClient()
    result = client.analyze_failure("yaml", "logs", "CI", "org/repo")

    assert result["root_cause"] == "Missing env var NODE_ENV"
    assert "fixed_yaml" in result

@patch("boto3.client")
def test_markdown_fenced_json_parsed(mock_boto):
    from app.services.bedrock_client import BedrockRemediationClient

    wrapped = "```json\n" + json.dumps({
        "root_cause": "r",
        "fixed_yaml": "y",
        "pr_title": "t",
        "pr_description": "d",
    }) + "\n```"

    mock_client = MagicMock()
    mock_client.converse.return_value = _make_bedrock_response(wrapped)
    mock_client.exceptions.ThrottlingException = type("ThrottlingException", (Exception,), {})
    mock_boto.return_value = mock_client

    client = BedrockRemediationClient()
    result = client.analyze_failure("yaml", "logs", "CI", "org/repo")
    assert result["root_cause"] == "r"

@patch("boto3.client")
def test_missing_required_key_raises(mock_boto):
    from app.services.bedrock_client import BedrockRemediationClient

    partial = json.dumps({"root_cause": "r", "fixed_yaml": "y"})

    mock_client = MagicMock()
    mock_client.converse.return_value = _make_bedrock_response(partial)
    mock_client.exceptions.ThrottlingException = type("ThrottlingException", (Exception,), {})
    mock_boto.return_value = mock_client

    client = BedrockRemediationClient()
    with pytest.raises(RuntimeError, match="missing keys"):
        client.analyze_failure("yaml", "logs", "CI", "org/repo")

@patch("boto3.client")
def test_invalid_json_raises(mock_boto):
    from app.services.bedrock_client import BedrockRemediationClient

    mock_client = MagicMock()
    mock_client.converse.return_value = _make_bedrock_response("not json at all")
    mock_client.exceptions.ThrottlingException = type("ThrottlingException", (Exception,), {})
    mock_boto.return_value = mock_client

    client = BedrockRemediationClient()
    with pytest.raises(RuntimeError):
        client.analyze_failure("yaml", "logs", "CI", "org/repo")

@patch("boto3.client")
def test_narrate_template_diff_returns_narrative(mock_boto):
    from app.services.bedrock_client import BedrockRemediationClient

    response = json.dumps({"narrative": "Missing the security-scan step is a real risk here."})
    mock_client = MagicMock()
    mock_client.converse.return_value = _make_bedrock_response(response)
    mock_client.exceptions.ThrottlingException = type("ThrottlingException", (Exception,), {})
    mock_boto.return_value = mock_client

    client = BedrockRemediationClient()
    diff = {
        "missing_components": ["./.github/workflows/_template-security-scan.yml"],
        "extra_components": [],
        "version_drift": [],
        "adoption_score": 50,
    }
    narrative = client.narrate_template_diff(diff, "ci.yml", "Standard CI")
    assert narrative == "Missing the security-scan step is a real risk here."

@patch("boto3.client")
def test_judge_pattern_cluster_match_returns_verdict(mock_boto):
    from app.services.bedrock_client import BedrockRemediationClient

    response = json.dumps({
        "is_match": True,
        "pattern_name": "build-test-push",
        "draft_template_yaml": "on: workflow_call\njobs:\n  build: {}\n",
    })
    mock_client = MagicMock()
    mock_client.converse.return_value = _make_bedrock_response(response)
    mock_client.exceptions.ThrottlingException = type("ThrottlingException", (Exception,), {})
    mock_boto.return_value = mock_client

    client = BedrockRemediationClient()
    verdict = client.judge_pattern_cluster(
        [{"job_key": "a.yml::build", "components": ["x", "y"]}], min_occurrences=3,
    )
    assert verdict is not None
    assert verdict["pattern_name"] == "build-test-push"
    assert "draft_template_yaml" in verdict

@patch("boto3.client")
def test_judge_pattern_cluster_no_match_returns_none(mock_boto):
    from app.services.bedrock_client import BedrockRemediationClient

    response = json.dumps({"is_match": False})
    mock_client = MagicMock()
    mock_client.converse.return_value = _make_bedrock_response(response)
    mock_client.exceptions.ThrottlingException = type("ThrottlingException", (Exception,), {})
    mock_boto.return_value = mock_client

    client = BedrockRemediationClient()
    verdict = client.judge_pattern_cluster(
        [{"job_key": "a.yml::build", "components": ["x", "y"]}], min_occurrences=3,
    )
    assert verdict is None

@patch("boto3.client")
def test_judge_pattern_cluster_match_missing_yaml_returns_none(mock_boto):
    from app.services.bedrock_client import BedrockRemediationClient

    response = json.dumps({"is_match": True, "pattern_name": "x"})
    mock_client = MagicMock()
    mock_client.converse.return_value = _make_bedrock_response(response)
    mock_client.exceptions.ThrottlingException = type("ThrottlingException", (Exception,), {})
    mock_boto.return_value = mock_client

    client = BedrockRemediationClient()
    verdict = client.judge_pattern_cluster(
        [{"job_key": "a.yml::build", "components": ["x", "y"]}], min_occurrences=3,
    )
    assert verdict is None

@patch("boto3.client")
def test_worker_decrypt_key_mismatch_raises(mock_boto):
    import base64, hashlib
    from cryptography.fernet import Fernet, InvalidToken
    from app.core.security import decrypt_token

    key_a = base64.urlsafe_b64encode(
        hashlib.sha256(b"stagecraft-token-encryption-v1:key-A").digest()
    )
    fernet_a = Fernet(key_a)
    encrypted = fernet_a.encrypt(b"ghp_token").decode()

    import app.core.config as cfg
    original_key = cfg.settings.SECRET_KEY
    cfg.settings.SECRET_KEY = "key-B"
    try:
        with pytest.raises(InvalidToken):
            decrypt_token(encrypted)
    finally:
        cfg.settings.SECRET_KEY = original_key
