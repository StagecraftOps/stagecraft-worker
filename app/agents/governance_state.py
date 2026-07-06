from __future__ import annotations
from typing import TypedDict

class GovernanceState(TypedDict, total=False):
    repo_owner: str
    repo_name: str
    workflow_file: str
    workflow_yaml: str
    governance_document_id: str
    retrieved_requirements: list[str]
    graph_context: dict
    findings: list[dict]
    agent_trace: list[str]
    error: str | None
