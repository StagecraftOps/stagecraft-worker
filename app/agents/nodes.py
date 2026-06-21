import asyncio
import json
import logging
import re
import time

import boto3
import yaml

from app.core.config import settings
from app.agents.state import AgentState
from app.services import mcp_client
from app.services.bedrock_client import _bedrock_boto3_kwargs

logger = logging.getLogger(__name__)

_MAX_RETRIES = 2
_MAX_TOOL_ROUNDS = 4

# Read-only GitHub MCP tools the root_cause step may call (via Converse tool
# use) to pull extra context. The worker injects the user's github_token; the
# model never sees it. Write tools (branch/commit/PR) are deliberately absent —
# the worker is read-only w.r.t. GitHub (writes happen only via the api-service
# Raise-PR flow after human review).
_GITHUB_TOOLS = {"get_workflow_yaml", "get_run_logs"}

_ROOT_CAUSE_TOOLCONFIG = {
    "tools": [
        {
            "toolSpec": {
                "name": "get_run_logs",
                "description": (
                    "Download the last 300 lines of logs for a GitHub Actions workflow run. "
                    "Call this when the truncated logs provided aren't enough to pinpoint the cause."
                ),
                "inputSchema": {"json": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string", "description": "Repository owner/org"},
                        "repo": {"type": "string", "description": "Repository name"},
                        "run_id": {"type": "integer", "description": "Workflow run ID"},
                    },
                    "required": ["owner", "repo", "run_id"],
                }},
            }
        },
        {
            "toolSpec": {
                "name": "get_workflow_yaml",
                "description": "Fetch a workflow YAML file from the repo at a git ref. Use to inspect a referenced or related workflow file.",
                "inputSchema": {"json": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "path": {"type": "string", "description": "Path under .github/workflows/"},
                        "ref": {"type": "string", "description": "Git ref (branch, tag, or commit SHA)"},
                    },
                    "required": ["owner", "repo", "path", "ref"],
                }},
            }
        },
    ]
}


def _bedrock_runtime():
    """A fresh bedrock-runtime client (cross-account creds are minted per call)."""
    return boto3.client("bedrock-runtime", region_name=settings.AWS_REGION, **_bedrock_boto3_kwargs())


def _parse_json(raw: str) -> dict | None:
    """Best-effort: pull the first JSON object out of a model response."""
    candidates = [raw]
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        candidates.append(m.group(0))
    for candidate in candidates:
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            continue
    return None


def _converse(prompt: str, max_tokens: int = 1024) -> str:
    """Single-shot Bedrock Converse (InvokeModel) call. Returns the model's text.

    Replaces the SCP-blocked InvokeAgent path — same role-specialized prompting,
    just a direct model call. Used by classify / security / pr_writer.
    """
    client = _bedrock_runtime()
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = client.converse(
                modelId=settings.BEDROCK_MODEL_ID,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={"maxTokens": max_tokens, "temperature": 0},
            )
            return resp["output"]["message"]["content"][0]["text"].strip()
        except client.exceptions.ThrottlingException:
            if attempt < _MAX_RETRIES:
                time.sleep(2 ** (attempt + 1))
                continue
            raise
    return ""


def _converse_with_tools(prompt: str, toolconfig: dict, tool_executor, max_rounds: int = _MAX_TOOL_ROUNDS) -> str:
    """Converse with native tool use: model -> (toolUse -> execute -> toolResult) -> ... -> final text.

    `tool_executor(name, input_dict) -> str` bridges the model's tool calls to
    the in-cluster MCP servers. Runs entirely on InvokeModel (SCP-allowed) — the
    same agentic loop the Bedrock-Agents Return-of-Control path did, but via the
    portable Converse tool-use API instead.
    """
    client = _bedrock_runtime()
    messages = [{"role": "user", "content": [{"text": prompt}]}]
    for _ in range(max_rounds):
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = client.converse(
                    modelId=settings.BEDROCK_MODEL_ID,
                    messages=messages,
                    toolConfig=toolconfig,
                    inferenceConfig={"maxTokens": 2048, "temperature": 0},
                )
                break
            except client.exceptions.ThrottlingException:
                if attempt < _MAX_RETRIES:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise

        out_msg = resp["output"]["message"]
        messages.append(out_msg)

        if resp.get("stopReason") == "tool_use":
            tool_results = []
            for block in out_msg.get("content", []):
                tu = block.get("toolUse")
                if not tu:
                    continue
                try:
                    result = tool_executor(tu["name"], tu.get("input", {}))
                except Exception as exc:
                    logger.warning("MCP tool %s failed: %s", tu.get("name"), exc)
                    result = f"ERROR: {exc}"
                tool_results.append({
                    "toolResult": {"toolUseId": tu["toolUseId"], "content": [{"text": str(result)[:6000]}]}
                })
            messages.append({"role": "user", "content": tool_results})
            continue

        return "".join(b.get("text", "") for b in out_msg.get("content", [])).strip()

    # Exceeded tool rounds — return whatever text the model last produced.
    return "".join(b.get("text", "") for b in messages[-1].get("content", []) if isinstance(b, dict)).strip()


_VALID_CATEGORIES = {
    "DEPENDENCY_VERSION", "AUTH_FAILURE", "NETWORK_TIMEOUT", "CONFIG_ERROR",
    "TEST_FAILURE", "BUILD_ERROR", "LINT_ERROR", "PERMISSION_ERROR", "UNKNOWN",
}


def classify_failure(state: AgentState) -> AgentState:
    prompt = (
        f"Workflow file: {state['workflow_file']}\n\n"
        f"Failure logs (scrubbed):\n{state['logs'][:4000]}\n\n"
        "Classify the failure. Respond with EXACTLY one of these tokens and nothing else:\n"
        "DEPENDENCY_VERSION | AUTH_FAILURE | NETWORK_TIMEOUT | CONFIG_ERROR | "
        "TEST_FAILURE | BUILD_ERROR | LINT_ERROR | PERMISSION_ERROR | UNKNOWN"
    )
    raw = _converse(prompt, max_tokens=20).upper().strip()
    category = raw if raw in _VALID_CATEGORIES else next((c for c in _VALID_CATEGORIES if c in raw), "UNKNOWN")
    trace = state.get("agent_trace", [])
    trace.append(f"classify_failure → {category}")
    return {**state, "failure_category": category, "agent_trace": trace}


def analyse_root_cause(state: AgentState) -> AgentState:
    github_token = state.get("github_token")

    def _exec(name: str, params: dict) -> str:
        if name not in _GITHUB_TOOLS:
            return f"ERROR: tool '{name}' is not available"
        call_params = {**params}
        if github_token:
            call_params["github_token"] = github_token
        return asyncio.run(mcp_client.call_tool(name, call_params))

    prompt = (
        f"Repository: {state['repo_owner']}/{state['repo_name']}\n"
        f"Workflow file: {state['workflow_file']}\n"
        f"Run ID: {state.get('run_id', '')}\n"
        f"Commit SHA: {state.get('head_sha', '')}\n"
        f"Failure category: {state['failure_category']}\n\n"
        f"Workflow YAML:\n{state['workflow_yaml'][:3000]}\n\n"
        f"Logs (truncated):\n{state['logs'][:4000]}\n\n"
        "Identify the SPECIFIC root cause of this failure. If the truncated logs above are "
        "not enough, call get_run_logs (use the Run ID above) to fetch the full logs before "
        'concluding. When done, respond ONLY with JSON: '
        '{"root_cause": "...", "severity": "low|medium|high|critical"}'
    )
    raw = _converse_with_tools(prompt, _ROOT_CAUSE_TOOLCONFIG, _exec)
    parsed = _parse_json(raw)
    if parsed:
        root_cause = parsed.get("root_cause", raw)
        severity = parsed.get("severity", "medium")
    else:
        root_cause = raw
        severity = "medium"
    trace = state.get("agent_trace", [])
    trace.append(f"analyse_root_cause → severity={severity}")
    return {**state, "root_cause": root_cause, "root_cause_severity": severity, "agent_trace": trace}


def _strip_fences(text: str) -> str:
    """Remove accidental markdown code fences from a model's YAML output."""
    t = text.strip()
    if t.startswith("```"):
        t = "\n".join(l for l in t.splitlines() if not l.strip().startswith("```")).strip()
    return t


def _validate_fix(original: str, fixed: str) -> tuple[bool, str]:
    """Validate a generated workflow fix. Returns (ok, reason).

    Catches the failure modes behind 'it produces junk': empty output, a
    capability refusal / prose (parses as a scalar, not a workflow mapping),
    malformed YAML, or an unchanged copy of the original (a non-fix).
    """
    if not fixed or not fixed.strip():
        return False, "empty output"
    try:
        parsed = yaml.safe_load(fixed)
    except yaml.YAMLError:
        return False, "invalid YAML syntax"
    # A real GitHub Actions workflow is a mapping with a top-level `jobs:` key.
    # (The `on:` key is parsed by YAML as the boolean True, so we key off jobs.)
    if not isinstance(parsed, dict) or "jobs" not in parsed:
        return False, "not a GitHub Actions workflow (no jobs:)"
    if fixed.strip() == original.strip():
        return False, "no change from original"
    return True, "ok"


def generate_fix(state: AgentState) -> AgentState:
    """Generate a corrected workflow YAML via the Bedrock Converse API.

    Converse is the primary (and only) path here — the yaml_fixer Bedrock Agent
    frequently returned a capability refusal ("I can't modify files") instead of
    YAML. Every candidate is validated as a real, *changed* GitHub Actions
    workflow before being stored; invalid output is rejected (suggested_yaml
    stays None) rather than surfaced to the user as a bogus fix.
    """
    from app.services.bedrock_client import BedrockRemediationClient

    trace = state.get("agent_trace", [])
    client = BedrockRemediationClient()
    original = state["workflow_yaml"]

    fixed = ""
    last_reason = "unknown"
    for attempt in range(2):
        candidate = _strip_fences(
            client.generate_yaml_fix(
                workflow_yaml=original,
                root_cause=state["root_cause"],
                failure_category=state.get("failure_category", "UNKNOWN"),
                logs=state.get("logs", ""),
            )
        )
        ok, reason = _validate_fix(original, candidate)
        if ok:
            fixed = candidate
            break
        last_reason = reason
        logger.warning("generate_fix attempt %d produced an invalid fix (%s)", attempt + 1, reason)

    if not fixed:
        # No safe, valid fix — store the analysis without a suggestion rather
        # than persisting junk. Downstream nodes handle a missing fix.
        trace.append(f"generate_fix → no valid fix produced ({last_reason})")
        return {**state, "suggested_yaml": None, "agent_trace": trace}

    trace.append("generate_fix → suggested_yaml produced (validated)")
    return {**state, "suggested_yaml": fixed, "agent_trace": trace}


def review_security(state: AgentState) -> AgentState:
    trace = state.get("agent_trace", [])
    if not state.get("suggested_yaml"):
        # No fix was produced — nothing to review.
        trace.append("review_security → skipped (no fix)")
        return {**state, "security_risk_score": 0, "security_findings": [], "agent_trace": trace}

    prompt = (
        f"Proposed workflow YAML fix:\n{state['suggested_yaml']}\n\n"
        "Review for security issues. Check: hardcoded secrets, missing SHA pins on actions, "
        "overbroad permissions, dangerous shell commands, untrusted registries.\n"
        'Respond ONLY with JSON: {"risk_score": 0-10, "findings": ["finding1", ...]}'
    )
    raw = _converse(prompt, max_tokens=512)
    parsed = _parse_json(raw)
    if parsed:
        try:
            risk_score = int(parsed.get("risk_score", 0))
        except (TypeError, ValueError):
            risk_score = 0
        findings = parsed.get("findings", []) or []
    else:
        risk_score = 0
        findings = []
    trace.append(f"review_security → risk_score={risk_score}, findings={len(findings)}")
    return {**state, "security_risk_score": risk_score, "security_findings": findings, "agent_trace": trace}


def write_pr_description(state: AgentState) -> AgentState:
    findings_text = "\n".join(f"- {f}" for f in state.get("security_findings", [])) or "None identified"
    prompt = (
        f"Root cause: {state['root_cause']}\n"
        f"Failure category: {state['failure_category']}\n"
        f"Security findings: {findings_text}\n\n"
        "Write a concise GitHub PR title and body for the AI-suggested fix. "
        'Respond ONLY with JSON: {"title": "fix: ...", "body": "## Root Cause\\n..."}'
    )
    raw = _converse(prompt, max_tokens=1024)
    parsed = _parse_json(raw)
    if parsed:
        pr_title = parsed.get("title", f"fix: AI remediation for {state['workflow_file']}")
        pr_description = parsed.get("body", f"## Root Cause\n{state['root_cause']}")
    else:
        pr_title = f"fix: AI remediation for {state['workflow_file']}"
        pr_description = f"## Root Cause\n{state['root_cause']}"
    trace = state.get("agent_trace", [])
    trace.append("write_pr_description → done")
    return {**state, "pr_title": pr_title, "pr_description": pr_description, "agent_trace": trace}


def should_block_high_risk(state: AgentState) -> str:
    if state.get("security_risk_score", 0) >= 8:
        return "block"
    return "approve"


def score_confidence(state: AgentState) -> AgentState:
    """Score how confident we are that the suggested fix is correct (0–100).

    Uses the Bedrock Converse API directly (not a Bedrock Agent) to avoid
    the system-prompt restrictions that plague the yaml_fixer agent.
    Falls back to a heuristic score if Bedrock is unavailable so this node
    never blocks the pipeline.
    """
    from app.services.bedrock_client import BedrockRemediationClient, _bedrock_boto3_kwargs

    trace = state.get("agent_trace", [])
    suggested = state.get("suggested_yaml", "")

    # If there is no YAML (blocked by security or empty), score 0
    if not suggested or state.get("error"):
        trace.append("score_confidence → 0 (no suggested YAML or pipeline error)")
        return {**state, "confidence_score": 0, "confidence_reasoning": "No fix was produced.", "agent_trace": trace}

    findings_text = "; ".join(state.get("security_findings", [])) or "none"

    prompt = (
        "You are a senior DevOps engineer reviewing an AI-generated GitHub Actions YAML fix.\n\n"
        f"FAILURE CATEGORY: {state.get('failure_category', 'UNKNOWN')}\n"
        f"ROOT CAUSE: {state.get('root_cause', '')}\n"
        f"SECURITY RISK SCORE: {state.get('security_risk_score', 0)}/10\n"
        f"SECURITY FINDINGS: {findings_text}\n\n"
        "ORIGINAL FAILING YAML:\n"
        f"{state.get('workflow_yaml', '')[:2000]}\n\n"
        "SUGGESTED FIX YAML:\n"
        f"{suggested[:2000]}\n\n"
        "Score your confidence that this fix correctly resolves the root cause WITHOUT introducing new problems.\n"
        "Consider: Does the fix directly address the root cause? Is it minimal? Are there any risks?\n\n"
        'Respond ONLY with JSON: {"score": <0-100>, "reasoning": "<one sentence>"}\n'
        "score=90-100: fix is clearly correct and minimal.\n"
        "score=70-89: likely correct but has minor uncertainty.\n"
        "score=50-69: fix is plausible but incomplete or broad.\n"
        "score=0-49: fix is wrong, risky, or doesn't address root cause."
    )

    try:
        client_obj = BedrockRemediationClient()
        response = client_obj._client.converse(
            modelId=client_obj._model_id,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 256},
        )
        raw: str = response["output"]["message"]["content"][0]["text"].strip()
        if raw.startswith("```"):
            raw = "\n".join(l for l in raw.splitlines() if not l.startswith("```")).strip()
        parsed = json.loads(raw)
        score = max(0, min(100, int(parsed.get("score", 50))))
        reasoning = parsed.get("reasoning", "")
    except Exception as exc:
        logger.warning("score_confidence failed, using heuristic: %s", exc)
        # Heuristic fallback: deduct from 80 based on security risk
        risk = state.get("security_risk_score", 0)
        score = max(10, 80 - (risk * 5))
        reasoning = "Heuristic score (Bedrock unavailable)."

    trace.append(f"score_confidence → {score}/100")
    return {**state, "confidence_score": score, "confidence_reasoning": reasoning, "agent_trace": trace}
