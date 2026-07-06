import asyncio
import logging
import re
import time

import boto3

from app.core.config import settings
from app.services import mcp_client
from app.services.bedrock_client import _bedrock_boto3_kwargs, _apply_bedrock_api_key

logger = logging.getLogger(__name__)

_MAX_RETRIES = 2
_MAX_TOOL_ROUNDS = 5

_TOOL_CONFIG = {
    "tools": [
        {
            "toolSpec": {
                "name": "search_remediations",
                "description": (
                    "Search past pipeline-failure remediation history. Use `query` for "
                    "semantic search (e.g. 'auth failures'), or repo_name/failure_category/"
                    "since_days when you already know what to narrow down to. Call this "
                    "more than once with different filters if the first search doesn't "
                    "give you enough to compare across repos or time."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Free-text semantic search, optional"},
                            "repo_name": {"type": "string", "description": "Filter to one repository, optional"},
                            "failure_category": {"type": "string", "description": "Filter to one category, optional"},
                            "since_days": {"type": "integer", "description": "Only runs in the last N days, optional"},
                            "limit": {"type": "integer", "description": "Max results, default 8"},
                        },
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "get_run_logs",
                "description": (
                    "Fetch the full failure logs for a specific GitHub Actions run. Only "
                    "use this if search_remediations' summary isn't enough — e.g. the user "
                    "asks for exact error text from one specific run."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "owner": {"type": "string"},
                            "repo": {"type": "string"},
                            "run_id": {"type": "integer"},
                        },
                        "required": ["owner", "repo", "run_id"],
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "get_workflow_yaml",
                "description": "Fetch the raw workflow YAML for a specific run, if needed to explain a fix.",
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "owner": {"type": "string"},
                            "repo": {"type": "string"},
                            "path": {"type": "string"},
                            "ref": {"type": "string"},
                        },
                        "required": ["owner", "repo", "path", "ref"],
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "query_graph",
                "description": (
                    "Query the CI/CD dependency/knowledge graph for structural facts about one "
                    "workflow — a graph traversal, not text search. Use this when the question is "
                    "about what's structurally connected to a workflow (what it depends on, what "
                    "depends on it, what governance rules or failure history are already linked "
                    "to it), rather than semantic search over remediation text. workflow_file can "
                    "be the colloquial name the user said (e.g. \"ci-auth-service\") -- it's matched "
                    "fuzzily server-side, you never need the exact .github/workflows/... path. "
                    "Omit repo_name unless the question specifies one."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "repo_name": {"type": "string", "description": "Optional -- omit if not specified in the question"},
                            "workflow_file": {"type": "string", "description": "Exact path or just a colloquial/partial name -- fuzzy-matched"},
                            "relationship": {
                                "type": "string",
                                "enum": ["depends_on", "depended_on_by", "governance", "failures"],
                                "description": "Which structural relationship to look up, default depends_on",
                            },
                        },
                        "required": ["workflow_file"],
                    }
                },
            }
        },
    ]
}

_SYSTEM_PROMPT = """You are Stagecraft's CI/CD investigator. Answer the user's question by calling \
search_remediations (and, only if needed, get_run_logs / get_workflow_yaml) to gather evidence \
from past pipeline failures, then reason across what you find — spot patterns, compare repos, \
explain trends. If the evidence doesn't support a confident answer, say so plainly instead of \
guessing. You have at most {max_rounds} tool calls — use them deliberately.

Call query_graph instead when the question is about structural dependencies or what's connected \
to a workflow (what it calls, what calls it, what governance rules or failures are already linked \
to it) — that's a graph traversal, not something semantic search over remediation text will answer well.

When citing evidence, refer to it the way a person would talk about it — "stagecraft-api's CI build \
step (analyzed 2026-06-20)" — never a bare remediation_id or UUID. If failure_category comes back \
UNKNOWN for everything relevant, say the categorization is missing/unreliable rather than citing \
UNKNOWN as if it were an answer.

Respond with ONLY your final answer in plain prose. Do not include any visible reasoning, \
<thinking> tags, or scratchpad text — think privately and output just the conclusion."""

def _bedrock_client():
    client = boto3.client(
        "bedrock-runtime",
        region_name=settings.AWS_REGION,
        **_bedrock_boto3_kwargs(),
    )
    _apply_bedrock_api_key(client)
    return client

_THINKING_BLOCK = re.compile(r"<thinking>.*?</thinking>", re.DOTALL | re.IGNORECASE)

def _strip_thinking(text: str) -> str:
    return _THINKING_BLOCK.sub("", text).strip()

def investigate(question: str, history: list[dict] | None = None) -> dict:
    client = _bedrock_client()

    prior: list[dict] = []
    for turn in (history or []):
        prior.append({
            "role": turn["role"],
            "content": [{"text": turn["content"]}],
        })

    while prior and prior[0]["role"] != "user":
        prior.pop(0)

    messages = prior + [
        {
            "role": "user",
            "content": [{"text": f"{_SYSTEM_PROMPT.format(max_rounds=_MAX_TOOL_ROUNDS)}\n\nQUESTION: {question}"}],
        }
    ]
    tool_calls: list[dict] = []
    assistant_content: list = []

    for _round in range(_MAX_TOOL_ROUNDS):
        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = client.converse(
                    modelId=settings.BEDROCK_MODEL_ID,
                    messages=messages,
                    toolConfig=_TOOL_CONFIG,
                    inferenceConfig={"maxTokens": 1024},
                )
                break
            except client.exceptions.ThrottlingException:
                if attempt < _MAX_RETRIES:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise

        stop_reason = response.get("stopReason", "")
        assistant_content = response["output"]["message"]["content"]
        messages.append({"role": "assistant", "content": assistant_content})

        if stop_reason != "tool_use":
            for block in assistant_content:
                if "text" in block:
                    return {"answer": _strip_thinking(block["text"]), "tool_calls": tool_calls}
            return {"answer": "", "tool_calls": tool_calls}

        tool_results = []
        for block in assistant_content:
            if "toolUse" not in block:
                continue
            tool_use = block["toolUse"]
            tool_name = tool_use["name"]
            tool_input = dict(tool_use.get("input", {}))
            tool_use_id = tool_use["toolUseId"]

            try:
                result_text = asyncio.run(mcp_client.call_tool(tool_name, tool_input))
                logger.info("Investigator tool %s succeeded (%d chars)", tool_name, len(result_text))
                tool_calls.append({"tool": tool_name, "input": tool_input, "ok": True})
            except Exception as exc:
                logger.warning("Investigator tool %s failed: %s", tool_name, exc)
                result_text = f"ERROR calling {tool_name}: {exc}"
                tool_calls.append({"tool": tool_name, "input": tool_input, "ok": False})

            tool_results.append({
                "toolResult": {
                    "toolUseId": tool_use_id,
                    "content": [{"text": result_text}],
                }
            })

        messages.append({"role": "user", "content": tool_results})

    logger.warning("investigate: hit max rounds (%d) for question %r", _MAX_TOOL_ROUNDS, question)
    for block in assistant_content:
        if "text" in block:
            return {"answer": _strip_thinking(block["text"]), "tool_calls": tool_calls}
    return {
        "answer": "I gathered some evidence but ran out of investigation steps before reaching a conclusion.",
        "tool_calls": tool_calls,
    }
