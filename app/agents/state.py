from __future__ import annotations
from typing import TypedDict

class AgentState(TypedDict, total=False):
    repo_owner: str
    repo_name: str
    workflow_file: str
    workflow_yaml: str
    logs: str
    head_sha: str
    run_id: int
    github_token: str
    failure_category: str
    app_context: dict | None
    root_cause: str
    root_cause_severity: str
    likely_code_level: bool
    code_level_reasoning: str
    suggested_yaml: str
    security_risk_score: int
    security_findings: list[str]
    pr_title: str
    pr_description: str
    confidence_score: int
    confidence_reasoning: str
    error: str | None
    agent_trace: list[str]
    fix_examples: list[str]
