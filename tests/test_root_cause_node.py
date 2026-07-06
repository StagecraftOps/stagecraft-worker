import os
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/x")
os.environ.setdefault("SECRET_KEY", "test-secret-only")

def _state() -> dict:
    return {
        "repo_owner": "Stagecraft-Ops",
        "repo_name": "stagecraft-api",
        "workflow_file": ".github/workflows/ci.yml",
        "workflow_yaml": "name: CI\njobs: {}",
        "logs": "Python version 99 was not found",
        "head_sha": "abc123",
        "run_id": 42,
        "failure_category": "DEPENDENCY_VERSION",
        "agent_trace": [],
    }

def test_root_cause_uses_direct_converse_when_mcp_is_disabled():
    from app.agents import nodes

    with patch.object(nodes.settings, "USE_MCP_TOOLS", False), \
         patch.object(
             nodes,
             "_converse",
             return_value='{"root_cause":"Python 99 is invalid","severity":"low"}',
         ) as converse, \
         patch.object(nodes, "_converse_with_tools") as with_tools:
        result = nodes.analyse_root_cause(_state())

    assert result["root_cause"] == "Python 99 is invalid"
    assert result["root_cause_severity"] == "low"
    converse.assert_called_once()
    with_tools.assert_not_called()

def test_root_cause_extracts_likely_code_level():
    from app.agents import nodes

    with patch.object(nodes.settings, "USE_MCP_TOOLS", False), \
         patch.object(
             nodes,
             "_converse",
             return_value=(
                 '{"root_cause":"missing pyproject.toml in service dir","severity":"high",'
                 '"likely_code_level":true,"code_level_reasoning":"packaging manifest absent"}'
             ),
         ):
        result = nodes.analyse_root_cause(_state())

    assert result["likely_code_level"] is True
    assert result["code_level_reasoning"] == "packaging manifest absent"
    assert "likely_code_level=True" in result["agent_trace"][-1]

def test_root_cause_defaults_likely_code_level_false():
    from app.agents import nodes

    with patch.object(nodes.settings, "USE_MCP_TOOLS", False), \
         patch.object(
             nodes,
             "_converse",
             return_value='{"root_cause":"bad secret name","severity":"medium"}',
         ):
        result = nodes.analyse_root_cause(_state())

    assert result["likely_code_level"] is False
    assert result["code_level_reasoning"] == ""

def test_app_context_included_in_prompt_when_present():
    from app.agents import nodes

    state = _state()
    state["app_context"] = {"language": "Go", "framework": "gin", "risk_tier": "critical", "regulatory_scope": ["PCI"]}

    with patch.object(nodes.settings, "USE_MCP_TOOLS", False), \
         patch.object(
             nodes,
             "_converse",
             return_value='{"root_cause":"x","severity":"low"}',
         ) as converse:
        nodes.analyse_root_cause(state)

    prompt = converse.call_args[0][0]
    assert "Go" in prompt and "critical" in prompt and "PCI" in prompt

def test_app_context_notes_included_in_prompt():
    from app.agents import nodes

    state = _state()
    state["app_context"] = {
        "language": "Go",
        "notes": "image-service Go module breaks when working-directory drifts from go.mod location",
    }

    with patch.object(nodes.settings, "USE_MCP_TOOLS", False), \
         patch.object(
             nodes,
             "_converse",
             return_value='{"root_cause":"x","severity":"low"}',
         ) as converse:
        nodes.analyse_root_cause(state)

    prompt = converse.call_args[0][0]
    assert "working-directory drifts from go.mod location" in prompt

def test_generate_fix_skips_bedrock_when_code_level_flagged():
    from app.agents import nodes

    state = _state()
    state["likely_code_level"] = True
    state["code_level_reasoning"] = "missing service directory"

    with patch("app.services.bedrock_client.BedrockRemediationClient") as client_cls:
        result = nodes.generate_fix(state)

    client_cls.assert_not_called()
    assert result["suggested_yaml"] == ""
    assert "skipped" in result["agent_trace"][-1]

def test_mcp_tool_schemas_match_github_mcp_server():
    from app.agents.nodes import _ROOT_CAUSE_TOOLCONFIG

    tools = {
        tool["toolSpec"]["name"]: tool["toolSpec"]["inputSchema"]["json"]
        for tool in _ROOT_CAUSE_TOOLCONFIG["tools"]
    }
    assert tools["get_run_logs"]["required"] == ["owner", "repo", "run_id"]
    assert tools["get_workflow_yaml"]["required"] == ["owner", "repo", "path", "ref"]
