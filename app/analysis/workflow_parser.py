import re

import yaml

_NEEDS_OUTPUT_PATTERN = re.compile(r"needs\.([a-zA-Z0-9_-]+)\.outputs\.([a-zA-Z0-9_-]+)")

def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []

def _find_needs_output_refs(value) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, str):
        refs.update(m.group(1) for m in _NEEDS_OUTPUT_PATTERN.finditer(value))
    elif isinstance(value, dict):
        for v in value.values():
            refs.update(_find_needs_output_refs(v))
    elif isinstance(value, list):
        for v in value:
            refs.update(_find_needs_output_refs(v))
    return refs

def _resolve_reusable_workflow_ref(uses: str) -> tuple[str, str, str | None]:
    if uses.startswith("./"):
        resolved_path = uses[2:]
        return "workflow", f"workflow::{resolved_path}", resolved_path
    return "reusable_workflow", f"reusable_workflow::{uses}", None

def parse_workflow(path: str, content: str) -> tuple[list[dict], list[dict]]:
    try:
        doc = yaml.safe_load(content)
    except yaml.YAMLError:
        return [], []
    if not isinstance(doc, dict):
        return [], []

    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return [], []

    on_block = doc.get("on") or doc.get(True) or {}
    triggers = list(on_block.keys()) if isinstance(on_block, dict) else _as_list(on_block)

    workflow_key = f"workflow::{path}"
    nodes: list[dict] = [{
        "node_type": "workflow",
        "external_key": workflow_key,
        "display_name": doc.get("name") or path,
        "workflow_file": path,
        "job_id": None,
        "metadata": {"triggers": triggers},
    }]
    edges: list[dict] = []

    job_keys = {job_id: f"job::{path}::{job_id}" for job_id in jobs if isinstance(jobs[job_id], dict)}

    for job_id, job_def in jobs.items():
        if not isinstance(job_def, dict):
            continue
        job_key = job_keys[job_id]
        strategy = job_def.get("strategy") or {}
        has_matrix = isinstance(strategy, dict) and "matrix" in strategy

        nodes.append({
            "node_type": "job",
            "external_key": job_key,
            "display_name": job_id,
            "workflow_file": path,
            "job_id": job_id,
            "metadata": {"runs_on": job_def.get("runs-on"), "matrix": has_matrix, "if": job_def.get("if")},
        })

        for needed in _as_list(job_def.get("needs")):
            if needed in job_keys:
                edges.append({
                    "source_key": job_keys[needed],
                    "target_key": job_key,
                    "edge_type": "needs",
                    "confidence": "certain",
                    "metadata": None,
                })

        for referenced_job in _find_needs_output_refs(job_def):
            if referenced_job in job_keys:
                edges.append({
                    "source_key": job_keys[referenced_job],
                    "target_key": job_key,
                    "edge_type": "needs_output",
                    "confidence": "certain",
                    "metadata": None,
                })

        uses = job_def.get("uses")
        if isinstance(uses, str):
            ref_node_type, reusable_key, ref_workflow_file = _resolve_reusable_workflow_ref(uses)
            nodes.append({
                "node_type": ref_node_type,
                "external_key": reusable_key,
                "display_name": uses,
                "workflow_file": ref_workflow_file,
                "job_id": None,

                "metadata": {"placeholder_reusable_ref": True} if ref_node_type == "workflow" else None,
            })
            edges.append({
                "source_key": job_key,
                "target_key": reusable_key,
                "edge_type": "matrix_fanout" if has_matrix else "uses_reusable",
                "confidence": "certain",
                "metadata": {
                    "with": job_def.get("with"),
                    "matrix": strategy.get("matrix") if has_matrix else None,
                },
            })

        for step in job_def.get("steps") or []:
            if not isinstance(step, dict):
                continue
            step_uses = step.get("uses")
            if isinstance(step_uses, str) and step_uses.startswith("./"):
                action_key = f"composite_action::{step_uses}"
                nodes.append({
                    "node_type": "composite_action",
                    "external_key": action_key,
                    "display_name": step_uses,
                    "workflow_file": None,
                    "job_id": None,
                    "metadata": None,
                })

                confidence = "ambiguous" if step.get("if") else "certain"
                edges.append({
                    "source_key": job_key,
                    "target_key": action_key,
                    "edge_type": "uses_composite",
                    "confidence": confidence,
                    "metadata": {"if": step.get("if"), "with": step.get("with")},
                })

    return nodes, edges
