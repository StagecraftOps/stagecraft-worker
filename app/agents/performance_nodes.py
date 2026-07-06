import json

from app.agents.compliance_nodes import _parse_json_list
from app.agents.nodes import _converse
from app.agents.performance_state import PerformanceState
from app.analysis.critical_path import compute_critical_path
from app.analysis.workflow_parser import parse_workflow

def propose_optimizations(state: PerformanceState) -> PerformanceState:
    bottlenecks = state.get("bottlenecks", [])
    candidates = state.get("parallelization_candidates", [])

    prompt = (
        f"You are optimizing the GitHub Actions workflow {state.get('workflow_file')} for "
        f"{state.get('repo_owner')}/{state.get('repo_name')}.\n\n"
        f"Detected bottlenecks (jobs on the critical path running slower than their own "
        f"historical p90):\n{json.dumps(bottlenecks, indent=2)}\n\n"
        f"Detected parallelization candidates (ordering constraints with no observed data "
        f"dependency):\n{json.dumps(candidates, indent=2)}\n\n"
        "Propose ranked, actionable optimizations. Respond with ONLY valid JSON: a list of "
        "objects in this exact format, ranked by impact (most impactful first):\n"
        '[{"type": "reorder"|"parallelize"|"split_matrix"|"cache"|"remove_redundant_step", '
        '"description": "<one sentence>", "estimated_time_savings_seconds": <integer>, '
        '"confidence_score": <0-100 integer>}]'
    )

    raw = _converse(prompt, max_tokens=1536)
    recommendations = _parse_json_list(raw)

    trace = state.get("agent_trace", [])
    trace.append(f"propose_optimizations: {len(recommendations)} recommendation(s)")
    return {**state, "recommendations": recommendations, "agent_trace": trace}

def draft_future_yaml(state: PerformanceState) -> PerformanceState:
    recommendations = state.get("recommendations", [])
    if not recommendations:
        trace = state.get("agent_trace", [])
        trace.append("draft_future_yaml: skipped, no recommendations")
        return {**state, "draft_future_yaml": None, "agent_trace": trace}

    top = recommendations[0]
    prompt = (
        f"Apply this single optimization to the workflow YAML below, changing only what's "
        f"necessary to implement it. Preserve everything else byte-for-byte.\n\n"
        f"Optimization: {top.get('description', '')}\n\n"
        f"Original workflow YAML:\n{state.get('workflow_yaml', '')[:8000]}\n\n"
        "Respond with ONLY the complete corrected workflow YAML as plain text — no prose, no "
        "code fences."
    )
    raw = _converse(prompt, max_tokens=4096)
    draft = raw.strip()
    if draft.startswith("```"):
        draft = "\n".join(line for line in draft.splitlines() if not line.strip().startswith("```")).strip()

    trace = state.get("agent_trace", [])
    trace.append("draft_future_yaml: drafted future-state YAML")
    return {**state, "draft_future_yaml": draft, "agent_trace": trace}

def simulate_savings(state: PerformanceState) -> PerformanceState:
    job_durations = state.get("job_durations", {})
    needs_edges = [tuple(edge) for edge in state.get("needs_edges", [])]

    baseline_jobs = [{"job_id": name, "duration_seconds": dur} for name, dur in job_durations.items()]
    baseline = compute_critical_path(baseline_jobs, needs_edges)

    draft_yaml = state.get("draft_future_yaml")
    if draft_yaml:
        _, parsed_edges = parse_workflow(state.get("workflow_file", "workflow.yml"), draft_yaml)
        new_needs_edges = [
            (e["source_key"].split("::")[-1], e["target_key"].split("::")[-1])
            for e in parsed_edges if e["edge_type"] == "needs"
        ]
        simulated = compute_critical_path(baseline_jobs, new_needs_edges)
    else:
        simulated = baseline

    trace = state.get("agent_trace", [])
    delta = baseline["total_duration_seconds"] - simulated["total_duration_seconds"]
    trace.append(f"simulate_savings: baseline={baseline['total_duration_seconds']}s, simulated={simulated['total_duration_seconds']}s, delta={delta}s")

    return {
        **state,
        "baseline_critical_path_seconds": baseline["total_duration_seconds"],
        "simulated_critical_path_seconds": simulated["total_duration_seconds"],
        "agent_trace": trace,
    }
