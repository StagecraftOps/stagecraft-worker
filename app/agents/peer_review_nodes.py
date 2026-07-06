import re

from app.agents.nodes import _converse, _parse_json
from app.agents.peer_review_state import PeerReviewState

_WORKFLOW_FILE_PATTERN = re.compile(r"^diff --git a/(\S+) b/\S+", re.MULTILINE)

def detect_workflow_changes(state: PeerReviewState) -> PeerReviewState:
    diff = state.get("diff", "")
    changed = [
        path for path in _WORKFLOW_FILE_PATTERN.findall(diff)
        if path.startswith(".github/workflows/") or path.startswith(".github/actions/")
    ]
    trace = state.get("agent_trace", [])
    trace.append(f"detect_workflow_changes: {len(changed)} CI file(s) touched")
    return {**state, "changed_workflow_files": changed, "agent_trace": trace}

def review_diff(state: PeerReviewState) -> PeerReviewState:
    diff = state.get("diff", "")
    changed_files = state.get("changed_workflow_files", [])

    prompt = (
        f"You are reviewing a pull request titled \"{state.get('pr_title', '')}\" for "
        f"{state.get('repo_owner')}/{state.get('repo_name')}.\n\n"
        f"CI/CD files changed in this PR: {changed_files or 'none'}\n\n"
        f"Full diff (truncated to 12000 chars):\n{diff[:12000]}\n\n"
        "Review this diff for CI/CD-relevant concerns: removed security/compliance steps, "
        "newly exposed secrets, overly broad permissions, unpinned action versions, and any "
        "logic errors in workflow YAML changes. Respond with ONLY valid JSON in this exact "
        "format:\n"
        '{"risk_score": <0-10 integer>, "findings": ["<finding 1>", "..."], '
        '"summary": "<one-paragraph review summary>"}'
    )

    raw = _converse(prompt, max_tokens=1024)
    parsed = _parse_json(raw)

    trace = state.get("agent_trace", [])
    trace.append(f"review_diff: risk_score={parsed.get('risk_score', 0)}")

    return {
        **state,
        "risk_score": int(parsed.get("risk_score", 0)),
        "findings": parsed.get("findings", []),
        "review_summary": parsed.get("summary", ""),
        "agent_trace": trace,
    }
