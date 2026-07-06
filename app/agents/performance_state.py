from __future__ import annotations
from typing import TypedDict

class PerformanceState(TypedDict, total=False):
    repo_owner: str
    repo_name: str
    workflow_file: str
    workflow_yaml: str
    bottlenecks: list[dict]
    parallelization_candidates: list[dict]
    job_durations: dict[str, int]
    needs_edges: list[list[str]]
    recommendations: list[dict]
    draft_future_yaml: str | None
    baseline_critical_path_seconds: int
    simulated_critical_path_seconds: int
    agent_trace: list[str]
    error: str | None
