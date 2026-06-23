"""Investigator Agent — answers "why"/comparative questions for Pipeline Chat.

Distinct from the remediation LangGraph (graph.py): that pipeline runs once
per failed workflow run, fully automated, and writes to a single
remediations row. This agent runs synchronously, once per chat message,
triggered on demand when a question needs reasoning across MULTIPLE past
runs rather than a single nearest-neighbor retrieval. It is read-only and
never reaches GitHub write tools.

Tool surface (all served by agora-mcp-github over the same in-cluster SSE
connection the remediation pipeline already uses):
  - search_remediations: query remediation history (semantic or filtered)
  - get_workflow_yaml / get_run_logs: pull a SPECIFIC run's raw data if the
    retrieved summary isn't enough to answer

Bounded at _MAX_TOOL_ROUNDS iterations so one chat message can't run away in
cost or latency.
"""
import asyncio
import logging
import time

import boto3

from app.core.config import settings
from app.services import mcp_client
from app.services.bedrock_client import _bedrock_boto3_kwargs

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
    ]
}

_SYSTEM_PROMPT = """You are aGorA's CI/CD investigator. Answer the user's question by calling \
search_remediations (and, only if needed, get_run_logs / get_workflow_yaml) to gather evidence \
from past pipeline failures, then reason across what you find — spot patterns, compare repos, \
explain trends. Always cite which remediation_id(s) your conclusions are based on. If the \
evidence doesn't support a confident answer, say so plainly instead of guessing. You have at \
most {max_rounds} tool calls — use them deliberately."""


def _bedrock_client():
    return boto3.client(
        "bedrock-runtime",
        region_name=settings.AWS_REGION,
        **_bedrock_boto3_kwargs(),
    )


def investigate(question: str) -> dict:
    """Run the bounded tool-calling loop and return the final answer + trace.

    Returns {"answer": str, "tool_calls": list[dict]} — tool_calls is a
    record of every search_remediations/get_run_logs/get_workflow_yaml call
    made, kept so the chat endpoint can surface "sources" the same way the
    single-shot RAG path already does.
    """
    client = _bedrock_client()
    messages = [
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
                    return {"answer": block["text"].strip(), "tool_calls": tool_calls}
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
            return {"answer": block["text"].strip(), "tool_calls": tool_calls}
    return {
        "answer": "I gathered some evidence but ran out of investigation steps before reaching a conclusion.",
        "tool_calls": tool_calls,
    }
