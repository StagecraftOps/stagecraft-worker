from __future__ import annotations
from typing import TypedDict


class ComplianceState(TypedDict, total=False):
    repo_owner: str
    repo_name: str
    workflow_file: str
    workflow_yaml: str
    framework: str  # e.g. "HIPAA", "PCI", "SOC2"
    graph_context: dict  # GraphRAG: existing_rules/known_failures/dependencies from Neo4j
    findings: list[dict]  # [{requirement_id, status, detail, severity}, ...]
    agent_trace: list[str]
    error: str | None
