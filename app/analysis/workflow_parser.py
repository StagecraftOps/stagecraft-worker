"""Parses one GitHub Actions workflow YAML file into graph nodes/edges.

Deterministic YAML parsing, not an LLM agent — lives in app/analysis/ rather
than app/agents/ to keep that package's AgentState/LangGraph convention clean.

Node/edge dicts here reference each other by external_key (a string), not a
DB id — graph_builder.py resolves external_key -> DB id when persisting.
"""
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
    """Recursively scan a job definition for needs.<job>.outputs.<x> references."""
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
    """Map a job's `uses:` reusable-workflow reference to a node identity.

    A local reference (`./path/to/workflow.yml` — always repo-root-relative
    per GHA semantics, never relative to the calling file's directory)
    resolves to the SAME external_key a `workflow` node for that physical
    file gets when it is itself parsed as a workflow file — so a caller's
    edge and the callee's own job graph become one bridgeable node instead
    of two disconnected nodes (today `workflow::<path>` and
    `reusable_workflow::./<path>` are unrelated nodes).

    An external/marketplace reference (`owner/repo/.github/workflows/x.yml@ref`)
    has no local file in this repo to bridge to, so it keeps its own
    `reusable_workflow` placeholder identity, unchanged from today.
    """
    if uses.startswith("./"):
        resolved_path = uses[2:]
        return "workflow", f"workflow::{resolved_path}", resolved_path
    return "reusable_workflow", f"reusable_workflow::{uses}", None


def parse_workflow(path: str, content: str) -> tuple[list[dict], list[dict]]:
    """Return (nodes, edges) for one workflow file. Returns ([], []) if unparsable."""
    try:
        doc = yaml.safe_load(content)
    except yaml.YAMLError:
        return [], []
    if not isinstance(doc, dict):
        return [], []

    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return [], []

    on_block = doc.get("on") or doc.get(True) or {}  # PyYAML parses bare `on:` key as True in some edge cases
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

        # Edge type 1: job-level `needs:`
        for needed in _as_list(job_def.get("needs")):
            if needed in job_keys:
                edges.append({
                    "source_key": job_keys[needed],
                    "target_key": job_key,
                    "edge_type": "needs",
                    "confidence": "certain",
                    "metadata": None,
                })

        # Edge type 5: needs.<job>.outputs.<x> data-dependency refs (in addition to plain needs:)
        for referenced_job in _find_needs_output_refs(job_def):
            if referenced_job in job_keys:
                edges.append({
                    "source_key": job_keys[referenced_job],
                    "target_key": job_key,
                    "edge_type": "needs_output",
                    "confidence": "certain",
                    "metadata": None,
                })

        # Edge types 2 & 4: job-level `uses:` — a reusable-workflow call (mutually
        # exclusive with `steps:` in GHA). `with:` is the parameter-passing edge;
        # a matrix-wrapped call fans out to one runtime invocation per matrix entry.
        uses = job_def.get("uses")
        if isinstance(uses, str):
            ref_node_type, reusable_key, ref_workflow_file = _resolve_reusable_workflow_ref(uses)
            nodes.append({
                "node_type": ref_node_type,
                "external_key": reusable_key,
                "display_name": uses,
                "workflow_file": ref_workflow_file,
                "job_id": None,
                # Marks this as a synthesized stand-in for a local workflow
                # file. build_graph_data's dedupe prefers the authoritative
                # `workflow` node (parsed when that file gets its own turn
                # in the repo's workflow_files loop) over this placeholder
                # when both share the same external_key.
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

        # Edge type 3: step-level `uses:` to a LOCAL composite action (./.github/actions/*).
        # Marketplace actions (owner/repo@ref) are external leaves, not graphed.
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
                # A step `if:` gated on a runtime value resolved elsewhere (e.g.
                # from service-config.json / file auto-detection) can't be
                # statically confirmed here — composite_action_resolver marks it.
                confidence = "ambiguous" if step.get("if") else "certain"
                edges.append({
                    "source_key": job_key,
                    "target_key": action_key,
                    "edge_type": "uses_composite",
                    "confidence": confidence,
                    "metadata": {"if": step.get("if"), "with": step.get("with")},
                })

    return nodes, edges
