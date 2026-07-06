import re

import yaml

_DISPATCH_PATTERN = re.compile(
    r"(?:curl|gh\s+api)[^\n]*"
    r"(?:https?://api\.github\.com/repos/|/repos/)?"
    r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/dispatches",
)

def find_dispatch_edges(path: str, content: str) -> tuple[list[dict], list[dict]]:
    try:
        doc = yaml.safe_load(content)
    except yaml.YAMLError:
        return [], []
    if not isinstance(doc, dict):
        return [], []

    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return [], []

    workflow_key = f"workflow::{path}"
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_targets: set[str] = set()

    for job_def in jobs.values():
        if not isinstance(job_def, dict):
            continue
        for step in job_def.get("steps") or []:
            if not isinstance(step, dict):
                continue
            run_body = step.get("run")
            if not isinstance(run_body, str):
                continue
            for match in _DISPATCH_PATTERN.finditer(run_body):
                owner, repo = match.group(1), match.group(2)
                target_key = f"external_repo::{owner}/{repo}"
                if target_key not in seen_targets:
                    seen_targets.add(target_key)
                    nodes.append({
                        "node_type": "external_repo",
                        "external_key": target_key,
                        "display_name": f"{owner}/{repo}",
                        "workflow_file": None,
                        "job_id": None,
                        "metadata": None,
                    })
                edges.append({
                    "source_key": workflow_key,
                    "target_key": target_key,
                    "edge_type": "repository_dispatch",
                    "confidence": "heuristic",
                    "metadata": None,
                })

    return nodes, edges
