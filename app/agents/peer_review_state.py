from __future__ import annotations
from typing import TypedDict

class PeerReviewState(TypedDict, total=False):
    repo_owner: str
    repo_name: str
    pr_number: int
    pr_title: str
    diff: str
    changed_workflow_files: list[str]
    risk_score: int
    findings: list[str]
    review_summary: str
    agent_trace: list[str]
    error: str | None
