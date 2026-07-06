import asyncio
import json
import logging
import re
import time
import uuid

import boto3
import yaml

from app.core.config import settings
from app.agents.state import AgentState
from app.services import mcp_client
from app.services.bedrock_client import _bedrock_boto3_kwargs, _apply_bedrock_api_key

logger = logging.getLogger(__name__)

_MAX_RETRIES = 2
_MAX_TOOL_ROUNDS = 6

_GITHUB_TOOLS = {"get_workflow_yaml", "get_run_logs"}

def _bedrock_client():
    client = boto3.client(
        "bedrock-runtime",
        region_name=settings.AWS_REGION,
        **_bedrock_boto3_kwargs(),
    )
    _apply_bedrock_api_key(client)
    return client

def _converse(prompt: str, max_tokens: int = 2048, temperature: float = 0.0) -> str:
    client = _bedrock_client()
    for attempt in range(_MAX_RETRIES + 1):
        try:
            response = client.converse(
                modelId=settings.BEDROCK_MODEL_ID,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
            )
            return response["output"]["message"]["content"][0]["text"].strip()
        except client.exceptions.ThrottlingException:
            if attempt < _MAX_RETRIES:
                time.sleep(2 ** (attempt + 1))
                continue
            raise

def _converse_with_tools(
    prompt: str,
    tool_config: dict,
    github_token: str | None = None,
    max_tokens: int = 4096,
) -> str:
    client = _bedrock_client()
    messages = [{"role": "user", "content": [{"text": prompt}]}]
    assistant_content: list = []

    for _round in range(_MAX_TOOL_ROUNDS):
        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = client.converse(
                    modelId=settings.BEDROCK_MODEL_ID,
                    messages=messages,
                    toolConfig=tool_config,
                    inferenceConfig={"maxTokens": max_tokens},
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
                    return block["text"].strip()
            return ""

        tool_results = []
        for block in assistant_content:
            if "toolUse" not in block:
                continue
            tool_use = block["toolUse"]
            tool_name = tool_use["name"]
            tool_input = dict(tool_use.get("input", {}))
            tool_use_id = tool_use["toolUseId"]

            if github_token and tool_name in _GITHUB_TOOLS:
                tool_input["github_token"] = github_token

            try:
                result_text = asyncio.run(mcp_client.call_tool(tool_name, tool_input))
                logger.info("MCP tool %s succeeded (%d chars)", tool_name, len(result_text))
            except Exception as exc:
                logger.warning("MCP tool %s failed: %s", tool_name, exc)
                result_text = f"ERROR calling {tool_name}: {exc}"

            tool_results.append({
                "toolResult": {
                    "toolUseId": tool_use_id,
                    "content": [{"text": result_text}],
                }
            })

        messages.append({"role": "user", "content": tool_results})

    logger.warning("_converse_with_tools: hit max rounds (%d), returning last text", _MAX_TOOL_ROUNDS)
    for block in assistant_content:
        if "text" in block:
            return block["text"].strip()
    return ""

_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.DOTALL)

def _parse_json(raw: str) -> dict:
    stripped = raw.strip()

    fence_match = _FENCE_RE.search(stripped)
    if fence_match:
        try:
            return json.loads(fence_match.group(1).strip())
        except json.JSONDecodeError:
            pass

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(stripped[start:end + 1])
        except json.JSONDecodeError:
            pass

    return {}

_ROOT_CAUSE_TOOLCONFIG = {
    "tools": [
        {
            "toolSpec": {
                "name": "get_run_logs",
                "description": (
                    "Fetch the full failure logs for a GitHub Actions workflow run. "
                    "Use when the truncated logs in the prompt are insufficient to determine root cause."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "owner": {"type": "string", "description": "GitHub org or user"},
                            "repo": {"type": "string", "description": "Repository name"},
                            "run_id": {"type": "integer", "description": "GitHub Actions run ID"},
                        },
                        "required": ["owner", "repo", "run_id"],
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "get_workflow_yaml",
                "description": (
                    "Fetch the raw workflow YAML file from GitHub. "
                    "Use when you need to inspect the full workflow definition."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "owner": {"type": "string", "description": "GitHub org or user"},
                            "repo": {"type": "string", "description": "Repository name"},
                            "path": {
                                "type": "string",
                                "description": "Workflow file path (e.g. .github/workflows/ci.yml)",
                            },
                            "ref": {
                                "type": "string",
                                "description": "Commit SHA or branch from which to read the workflow",
                            },
                        },
                        "required": ["owner", "repo", "path", "ref"],
                    }
                },
            }
        },
    ]
}

def classify_failure(state: AgentState) -> AgentState:
    prompt = (
        f"Workflow file: {state['workflow_file']}\n\n"
        f"Failure logs (scrubbed):\n{state['logs'][:4000]}\n\n"
        "Classify the failure. Respond with exactly one of: "
        "DEPENDENCY_VERSION | AUTH_FAILURE | NETWORK_TIMEOUT | CONFIG_ERROR | "
        "TEST_FAILURE | BUILD_ERROR | LINT_ERROR | PERMISSION_ERROR | UNKNOWN"
    )
    raw = _converse(prompt, max_tokens=32)
    valid = {"DEPENDENCY_VERSION", "AUTH_FAILURE", "NETWORK_TIMEOUT", "CONFIG_ERROR",
             "TEST_FAILURE", "BUILD_ERROR", "LINT_ERROR", "PERMISSION_ERROR", "UNKNOWN"}
    category = raw.upper().strip() if raw.upper().strip() in valid else "UNKNOWN"
    trace = state.get("agent_trace", [])
    trace.append(f"classify_failure → {category}")
    return {**state, "failure_category": category, "agent_trace": trace}

def _app_context_block(app_context: dict | None) -> str:
    if not app_context:
        return "Application context: none on file for this repo.\n\n"
    block = (
        f"Application context: language={app_context.get('language') or 'unknown'}, "
        f"framework={app_context.get('framework') or 'unknown'}, "
        f"risk_tier={app_context.get('risk_tier') or 'unset'}, "
        f"regulatory_scope={app_context.get('regulatory_scope') or []}\n"
    )
    notes = (app_context.get("notes") or "").strip()
    if notes:
        block += (
            "Known failure modes / architecture notes for this application "
            f"(from its uploaded context file):\n{notes[:2000]}\n"
        )
    return block + "\n"

def analyse_root_cause(state: AgentState) -> AgentState:
    prompt = (
        f"Repository: {state['repo_owner']}/{state['repo_name']}\n"
        f"Run ID: {state.get('run_id', '')}\n"
        f"Failure category: {state['failure_category']}\n\n"
        f"{_app_context_block(state.get('app_context'))}"
        f"Workflow YAML:\n{state['workflow_yaml'][:3000]}\n\n"
        f"Logs:\n{state['logs'][:4000]}\n\n"
        "Identify the specific root cause. If MCP enrichment is enabled and the supplied "
        "context is insufficient, call get_run_logs with owner, repo, and run_id; or call "
        "get_workflow_yaml with owner, repo, path, and ref.\n\n"
        "Also judge whether this failure is fixable by changing the WORKFLOW YAML (a pipeline "
        "misconfiguration -- wrong secret name, missing permissions, bad runner OS, invalid version "
        "pin, wrong working-directory) versus something that can only be fixed by changing the "
        "APPLICATION'S OWN SOURCE CODE OR REPOSITORY CONTENT (a missing/malformed packaging manifest, "
        "a real failing test assertion, a missing service directory, a genuine application logic bug). "
        "Use the application context above (language/framework) to inform this judgment when relevant. "
        'Respond in JSON: {"root_cause": "...", "severity": "low|medium|high|critical", '
        '"likely_code_level": true|false, "code_level_reasoning": "one sentence, empty string if likely_code_level is false"}'
    )
    if settings.USE_MCP_TOOLS:
        raw = _converse_with_tools(
            prompt,
            tool_config=_ROOT_CAUSE_TOOLCONFIG,
            github_token=state.get("github_token"),
            max_tokens=1024,
        )
    else:
        logger.info("MCP enrichment disabled; using fetched workflow and log context")
        raw = _converse(prompt, max_tokens=1024)
    parsed = _parse_json(raw)
    root_cause = parsed.get("root_cause", raw) if parsed else raw
    severity = parsed.get("severity", "medium") if parsed else "medium"
    likely_code_level = bool(parsed.get("likely_code_level", False)) if parsed else False
    code_level_reasoning = parsed.get("code_level_reasoning", "") if parsed else ""
    trace = state.get("agent_trace", [])
    trace.append(
        f"analyse_root_cause → severity={severity}"
        + (f", likely_code_level=True ({code_level_reasoning})" if likely_code_level else "")
    )
    return {
        **state,
        "root_cause": root_cause,
        "root_cause_severity": severity,
        "likely_code_level": likely_code_level,
        "code_level_reasoning": code_level_reasoning,
        "agent_trace": trace,
    }

def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = "\n".join(l for l in t.splitlines() if not l.strip().startswith("```")).strip()
    return t

_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF"
    "\U00002B00-\U00002BFF"
    "\U0000FE0F"
    "]+"
)

def _strip_emojis(text: str) -> str:
    return _EMOJI_PATTERN.sub("", text)

_INLINE_COMMENT = re.compile(r"(?<!['\"])\s#[^\n]*$")

def _strip_hallucinated_comments(original: str, candidate: str) -> str:
    original_comment_lines = {
        line.strip() for line in original.splitlines() if line.strip().startswith("#")
    }
    original_text = original

    kept = []
    for line in candidate.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            if stripped not in original_comment_lines:
                continue
            kept.append(line)
            continue

        if "#" in line and line not in original_text.splitlines():
            quote_parity_ok = line.count('"') % 2 == 0 and line.count("'") % 2 == 0
            match = _INLINE_COMMENT.search(line)
            if quote_parity_ok and match and match.group(0).strip() not in original_text:
                line = line[: match.start()]
        kept.append(line)
    return "\n".join(kept)

def _validate_fix(original: str, fixed: str) -> tuple[bool, str]:
    if not fixed or not fixed.strip():
        return False, "empty output"
    try:
        parsed = yaml.safe_load(fixed)
    except yaml.YAMLError:
        return False, "invalid YAML syntax"
    if not isinstance(parsed, dict) or "jobs" not in parsed:
        return False, "not a GitHub Actions workflow (no jobs:)"
    if fixed.strip() == original.strip():
        return False, "no change from original"
    return True, "ok"

def generate_fix(state: AgentState) -> AgentState:
    from app.services.bedrock_client import BedrockRemediationClient
    import yaml

    trace = state.get("agent_trace", [])

    if state.get("likely_code_level"):
        trace.append(
            "generate_fix → skipped: root cause flagged as application-code-level, "
            "not a pipeline YAML fix"
        )
        return {**state, "suggested_yaml": "", "agent_trace": trace}

    client = BedrockRemediationClient()
    original = state["workflow_yaml"]

    compressed_yaml = original[:8000] if len(original) > 8000 else original

    fix_examples = state.get("fix_examples") or []
    few_shot_block = ""
    if fix_examples:
        few_shot_block = "\n\n".join(
            f"ACCEPTED FIX EXAMPLE {i + 1}:\n```yaml\n{ex}\n```"
            for i, ex in enumerate(fix_examples[:2])
        )
        trace.append(f"generate_fix → injecting {len(fix_examples[:2])} few-shot example(s) from fix_memories")

    fixed = ""
    last_error_context = "unknown"
    candidate = None

    for attempt in range(3):
        if attempt == 0:
            logger.info("Attempt 1: Generating initial YAML fix from Bedrock")
            try:
                raw_candidate = client.generate_yaml_fix(
                    workflow_yaml=compressed_yaml,
                    root_cause=state["root_cause"],
                    failure_category=state.get("failure_category", "UNKNOWN"),
                    logs=state.get("logs", ""),
                    few_shot_context=few_shot_block,
                )
            except Exception as exc:
                last_error_context = f"Bedrock invocation failed: {str(exc)}"
                logger.warning(f"generate_fix attempt {attempt + 1} Bedrock error: {last_error_context}")
                continue
        else:
            logger.info(f"Attempt {attempt + 1}: Self-correcting malformed YAML fix")
            try:
                raw_candidate = client.correct_yaml_syntax(
                    original_yaml=original,
                    malformed_yaml=candidate,
                    error_message=last_error_context
                )
            except Exception as exc:
                last_error_context = f"Bedrock correction invocation failed: {str(exc)}"
                logger.warning(f"generate_fix attempt {attempt + 1} Bedrock error: {last_error_context}")
                continue

        candidate = _strip_fences(raw_candidate)
        candidate = _strip_emojis(candidate)
        candidate = _strip_hallucinated_comments(original, candidate)

        import re
        lines = []
        spacing_pattern = re.compile(r"^([ \t]*[a-zA-Z0-9_-]+):(?!\s)(.+)$")
        for line in candidate.splitlines():
            m = spacing_pattern.match(line)
            if m:
                lines.append(f"{m.group(1)}: {m.group(2)}")
            else:
                lines.append(line)
        candidate = "\n".join(lines)

        if not candidate or not candidate.strip():
            last_error_context = "Validation failed: Suggested YAML is empty."
            logger.warning(f"generate_fix attempt {attempt + 1} validation failed: {last_error_context}")
            continue

        if candidate.strip() == original.strip():
            last_error_context = "Validation failed: Suggested YAML is identical to the original workflow; no change was made."
            logger.warning(f"generate_fix attempt {attempt + 1} validation failed: {last_error_context}")
            continue

        try:
            parsed = yaml.safe_load(candidate)
            if not isinstance(parsed, dict) or "jobs" not in parsed:
                last_error_context = "Validation failed: Root key 'jobs' was not found in the generated workflow YAML."
                logger.warning(f"generate_fix attempt {attempt + 1} validation failed: {last_error_context}")
                continue

            fixed = candidate
            break

        except yaml.YAMLError as exc:
            last_error_context = f"YAML syntax / parsing error:\n{str(exc)}"
            logger.warning(f"generate_fix attempt {attempt + 1} validation failed with YAMLError: {last_error_context}")

    if not fixed:
        trace.append("generate_fix → self-correction loop failed to produce valid YAML")
        return {**state, "suggested_yaml": None, "agent_trace": trace}

    trace.append(f"generate_fix → suggested_yaml produced on attempt {attempt + 1} (validated)")
    return {**state, "suggested_yaml": fixed, "agent_trace": trace}

def review_security(state: AgentState) -> AgentState:
    trace = state.get("agent_trace", [])
    if not state.get("suggested_yaml"):
        trace.append("review_security → skipped (no fix)")
        return {**state, "security_risk_score": 0, "security_findings": [], "agent_trace": trace}

    prompt = (
        f"Proposed workflow YAML fix:\n{state['suggested_yaml']}\n\n"
        "Review for security issues. Check: hardcoded secrets, missing SHA pins on actions, "
        "overbroad permissions, dangerous shell commands, untrusted registries.\n"
        'Respond in JSON: {"risk_score": 0-10, "findings": ["finding1", ...]}'
    )
    raw = _converse(prompt, max_tokens=512)
    parsed = _parse_json(raw)
    try:
        risk_score = int(parsed.get("risk_score", 0))
        findings = parsed.get("findings", [])
    except (ValueError, AttributeError):
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
        'Respond in JSON: {"title": "fix: ...", "body": "## Root Cause\\n..."}'
    )
    raw = _converse(prompt, max_tokens=512)
    parsed = _parse_json(raw)
    pr_title = (
        parsed.get("title", f"fix: AI remediation for {state['workflow_file']}")
        if parsed else f"fix: AI remediation for {state['workflow_file']}"
    )
    pr_description = (
        parsed.get("body", f"## Root Cause\n{state['root_cause']}")
        if parsed else f"## Root Cause\n{state['root_cause']}"
    )
    trace = state.get("agent_trace", [])
    trace.append("write_pr_description → done")
    return {**state, "pr_title": pr_title, "pr_description": pr_description, "agent_trace": trace}

def should_block_high_risk(state: AgentState) -> str:
    if state.get("security_risk_score", 0) >= 8:
        return "block"
    return "approve"

def score_confidence(state: AgentState) -> AgentState:
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
