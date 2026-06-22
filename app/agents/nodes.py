import asyncio
import json
import logging
import time
import uuid

import boto3
import yaml

from app.core.config import settings
from app.agents.state import AgentState
from app.services import mcp_client
from app.services.bedrock_client import _bedrock_boto3_kwargs

logger = logging.getLogger(__name__)

_MAX_RETRIES = 2
_MAX_ROC_ROUNDS = 6

# Read-only GitHub tools the agents may call via Return-of-Control. The worker
# injects the user's OAuth token (the action-group schema has no notion of a
# "current user"). Write tools (branch/commit/PR) are deliberately NOT exposed:
# the worker is read-only w.r.t. GitHub by design (writes happen only via the
# api-service Raise-PR flow after a human reviews the suggestion).
_GITHUB_TOOLS = {"get_workflow_yaml", "get_run_logs"}


def _invoke_bedrock_agent(agent_key: str, session_id: str, prompt: str, github_token: str | None = None) -> str:
    """Invoke a Bedrock agent, resolving any Return-of-Control tool calls via MCP.

    The agent may stream a `returnControl` event instead of a final completion when
    its action group decides a tool needs to run. We execute that tool against the
    in-cluster MCP server and re-invoke the agent with the result, looping until the
    agent returns a normal completion. MCP failures are caught and fed back as an
    error result so a flaky tool never breaks the analysis.
    """
    client = boto3.client(
        "bedrock-agent-runtime",
        region_name=settings.AWS_REGION,
        **_bedrock_boto3_kwargs(),
    )
    agent_id = settings.BEDROCK_AGENT_IDS.get(agent_key, "")
    agent_alias_id = settings.BEDROCK_AGENT_ALIAS_IDS.get(agent_key, "TSTALIASID")

    session_state: dict | None = None
    for _round in range(_MAX_ROC_ROUNDS):
        for attempt in range(_MAX_RETRIES + 1):
            try:
                kwargs = {"agentId": agent_id, "agentAliasId": agent_alias_id, "sessionId": session_id}
                if session_state is not None:
                    kwargs["sessionState"] = session_state
                else:
                    kwargs["inputText"] = prompt
                response = client.invoke_agent(**kwargs)
                break
            except client.exceptions.ThrottlingException:
                if attempt < _MAX_RETRIES:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise

        completion = ""
        return_control = None
        for event in response["completion"]:
            if "chunk" in event:
                completion += event["chunk"]["bytes"].decode()
            elif "returnControl" in event:
                return_control = event["returnControl"]

        if return_control is None:
            return completion.strip()

        results = []
        for invocation_input in return_control["invocationInputs"]:
            func_input = invocation_input["functionInvocationInput"]
            function_name = func_input["function"]
            action_group = func_input["actionGroup"]
            params = {p["name"]: p["value"] for p in func_input.get("parameters", [])}
            if github_token and function_name in _GITHUB_TOOLS:
                params["github_token"] = github_token

            try:
                tool_result = asyncio.run(mcp_client.call_tool(function_name, params))
            except Exception as exc:
                logger.warning("MCP tool %s failed: %s", function_name, exc)
                tool_result = f"ERROR: {exc}"

            results.append({
                "functionResult": {
                    "actionGroup": action_group,
                    "function": function_name,
                    "responseBody": {"TEXT": {"body": tool_result}},
                }
            })

        session_state = {
            "invocationId": return_control["invocationId"],
            "returnControlInvocationResults": results,
        }

    raise RuntimeError(f"Agent '{agent_key}' exceeded {_MAX_ROC_ROUNDS} Return-of-Control rounds")


def classify_failure(state: AgentState) -> AgentState:
    session_id = str(uuid.uuid4())
    prompt = (
        f"Workflow file: {state['workflow_file']}\n\n"
        f"Failure logs (scrubbed):\n{state['logs'][:4000]}\n\n"
        "Classify the failure. Respond with exactly one of: "
        "DEPENDENCY_VERSION | AUTH_FAILURE | NETWORK_TIMEOUT | CONFIG_ERROR | "
        "TEST_FAILURE | BUILD_ERROR | LINT_ERROR | PERMISSION_ERROR | UNKNOWN"
    )
    category = _invoke_bedrock_agent("classifier", session_id, prompt)
    valid = {"DEPENDENCY_VERSION", "AUTH_FAILURE", "NETWORK_TIMEOUT", "CONFIG_ERROR",
             "TEST_FAILURE", "BUILD_ERROR", "LINT_ERROR", "PERMISSION_ERROR", "UNKNOWN"}
    category = category.upper().strip() if category.upper().strip() in valid else "UNKNOWN"
    trace = state.get("agent_trace", [])
    trace.append(f"classify_failure → {category}")
    return {**state, "failure_category": category, "agent_trace": trace}


def analyse_root_cause(state: AgentState) -> AgentState:
    session_id = str(uuid.uuid4())
    prompt = (
        f"Repository: {state['repo_owner']}/{state['repo_name']}\n"
        f"Run ID: {state.get('run_id', '')}\n"
        f"Failure category: {state['failure_category']}\n\n"
        f"Workflow YAML:\n{state['workflow_yaml'][:3000]}\n\n"
        f"Logs:\n{state['logs'][:4000]}\n\n"
        "Identify the specific root cause. Use the github-tools action group (get_run_logs "
        "with the Run ID above) if you need the full logs. Respond in JSON: "
        '{"root_cause": "...", "severity": "low|medium|high|critical"}'
    )
    raw = _invoke_bedrock_agent("root_cause", session_id, prompt, github_token=state.get("github_token"))
    try:
        parsed = json.loads(raw)
        root_cause = parsed.get("root_cause", raw)
        severity = parsed.get("severity", "medium")
    except json.JSONDecodeError:
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

    Kept on Converse (not the yaml_fixer Bedrock Agent) on purpose: the agent
    frequently returns a capability refusal ("I can't modify files") instead of
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

    session_id = str(uuid.uuid4())
    prompt = (
        f"Proposed workflow YAML fix:\n{state['suggested_yaml']}\n\n"
        "Review for security issues. Check: hardcoded secrets, missing SHA pins on actions, "
        "overbroad permissions, dangerous shell commands, untrusted registries.\n"
        'Respond in JSON: {"risk_score": 0-10, "findings": ["finding1", ...]}'
    )
    raw = _invoke_bedrock_agent("security_reviewer", session_id, prompt)
    try:
        parsed = json.loads(raw)
        risk_score = int(parsed.get("risk_score", 0))
        findings = parsed.get("findings", [])
    except (json.JSONDecodeError, ValueError):
        risk_score = 0
        findings = []
    trace.append(f"review_security → risk_score={risk_score}, findings={len(findings)}")
    return {**state, "security_risk_score": risk_score, "security_findings": findings, "agent_trace": trace}


def write_pr_description(state: AgentState) -> AgentState:
    session_id = str(uuid.uuid4())
    findings_text = "\n".join(f"- {f}" for f in state.get("security_findings", [])) or "None identified"
    prompt = (
        f"Root cause: {state['root_cause']}\n"
        f"Failure category: {state['failure_category']}\n"
        f"Security findings: {findings_text}\n\n"
        "Write a concise GitHub PR title and body for the AI-suggested fix. "
        'Respond in JSON: {"title": "fix: ...", "body": "## Root Cause\\n..."}'
    )
    raw = _invoke_bedrock_agent("pr_writer", session_id, prompt)
    try:
        parsed = json.loads(raw)
        pr_title = parsed.get("title", f"fix: AI remediation for {state['workflow_file']}")
        pr_description = parsed.get("body", f"## Root Cause\n{state['root_cause']}")
    except json.JSONDecodeError:
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

    Uses the Bedrock Converse API directly (not a Bedrock Agent) to avoid the
    system-prompt restrictions that plague the yaml_fixer agent. Falls back to a
    heuristic score if Bedrock is unavailable so this node never blocks the pipeline.
    """
    from app.services.bedrock_client import BedrockRemediationClient

    trace = state.get("agent_trace", [])
    suggested = state.get("suggested_yaml", "")

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
        risk = state.get("security_risk_score", 0)
        score = max(10, 80 - (risk * 5))
        reasoning = "Heuristic score (Bedrock unavailable)."

    trace.append(f"score_confidence → {score}/100")
    return {**state, "confidence_score": score, "confidence_reasoning": reasoning, "agent_trace": trace}
