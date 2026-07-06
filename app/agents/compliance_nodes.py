import json

from app.agents.compliance_state import ComplianceState
from app.agents.graph_context import format_graph_context_block
from app.agents.nodes import _converse

_FRAMEWORK_CONTROLS = {
    "HIPAA": [
        "secret-scanning step present (PHI credentials must never be committed)",
        "encryption-at-rest / encryption-in-transit validation for any data-handling job",
        "access-control / least-privilege review for deployment permissions",
        "audit-logging step for any job that touches production data",
    ],
    "PCI": [
        "secret-scanning step present (cardholder data / API keys)",
        "dependency vulnerability scanning step present",
        "network segmentation / firewall validation for deployment jobs",
        "mandatory security review gate before production deploy",
    ],
    "SOC2": [
        "PR review requirement enforced (no direct pushes to protected branches)",
        "audit-logging step for deployment jobs",
        "automated security scanning step present",
        "change-management approval gate before production deploy",
    ],
}

def check_framework_controls(state: ComplianceState) -> ComplianceState:
    framework = state.get("framework", "")
    controls = _FRAMEWORK_CONTROLS.get(framework.upper(), [])
    control_list = "\n".join(f"- {c}" for c in controls) or "- (no predefined controls for this framework)"

    prompt = (
        f"You are auditing a GitHub Actions workflow ({state.get('workflow_file')}) for "
        f"{framework} compliance.\n\n"
        f"Expected controls for {framework}:\n{control_list}\n\n"
        f"Structural graph context (existing audit/dependency state for this workflow):\n"
        f"{format_graph_context_block(state)}\n\n"
        f"Workflow YAML:\n{state.get('workflow_yaml', '')[:8000]}\n\n"
        "For each expected control, determine whether the workflow satisfies it. Use the "
        "structural graph context to note continuity (e.g. a control already governing this "
        "workflow, or a known failure history) where relevant to your detail. Respond with "
        "ONLY valid JSON: a list of objects, one per control, in this exact format:\n"
        '[{"requirement_id": "<short id>", "status": "compliant"|"gap"|"not_applicable", '
        '"detail": "<one sentence>", "severity": "low"|"medium"|"high", '
        '"remediation_suggestion": "<one sentence, empty string if compliant>"}]'
    )

    raw = _converse(prompt, max_tokens=1536)
    parsed = _parse_json_list(raw)

    trace = state.get("agent_trace", [])
    gaps = sum(1 for f in parsed if f.get("status") == "gap")
    trace.append(f"check_framework_controls: {framework} — {gaps} gap(s) of {len(parsed)} control(s)")

    return {**state, "findings": parsed, "agent_trace": trace}

def _parse_json_list(raw: str) -> list[dict]:
    stripped = raw.strip()
    if stripped.startswith("```"):
        stripped = "\n".join(
            line for line in stripped.splitlines() if not line.strip().startswith("```")
        ).strip()
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        start = stripped.find("[")
        end = stripped.rfind("]")
        if start != -1 and end > start:
            try:
                parsed = json.loads(stripped[start:end + 1])
                return parsed if isinstance(parsed, list) else []
            except json.JSONDecodeError:
                pass
        return []
