"""Deterministic DAG longest-path computation over a run's jobs.

Pure computation, no AI — per PRD FR-2, runtime monitoring must work "without
requiring AI analysis." Given each job's duration and the needs: dependency
edges parsed from the workflow YAML at the run's commit, this computes:
  - the critical path (the job chain whose combined duration determines the
    run's total wall-clock time)
  - the single longest-running job
"""


def compute_critical_path(
    jobs: list[dict], needs_edges: list[tuple[str, str]]
) -> dict:
    """
    jobs: [{"job_id": <workflow-yaml job id>, "duration_seconds": int}, ...]
    needs_edges: [(from_job_id, to_job_id), ...] meaning from_job_id must
        finish before to_job_id starts.

    Returns {"critical_path_job_ids": [job_id, ...], "total_duration_seconds": int,
             "longest_job_id": job_id or None}. Jobs not present in `jobs` are
    ignored. Returns an empty result if `jobs` is empty.
    """
    if not jobs:
        return {"critical_path_job_ids": [], "total_duration_seconds": 0, "longest_job_id": None}

    durations = {j["job_id"]: j.get("duration_seconds") or 0 for j in jobs}
    predecessors: dict[str, list[str]] = {job_id: [] for job_id in durations}
    successors: dict[str, list[str]] = {job_id: [] for job_id in durations}

    for source, target in needs_edges:
        if source in durations and target in durations:
            predecessors[target].append(source)
            successors[source].append(target)

    # Kahn's algorithm for topological order (silently drops any cycle rather
    # than raising — a malformed workflow shouldn't crash runtime monitoring).
    in_degree = {job_id: len(preds) for job_id, preds in predecessors.items()}
    queue = [job_id for job_id, deg in in_degree.items() if deg == 0]
    topo_order: list[str] = []
    while queue:
        node = queue.pop(0)
        topo_order.append(node)
        for succ in successors[node]:
            in_degree[succ] -= 1
            if in_degree[succ] == 0:
                queue.append(succ)
    # Any node left with in_degree > 0 is part of a cycle — append it anyway
    # so it's not silently dropped from the duration calculation.
    for job_id, deg in in_degree.items():
        if deg > 0 and job_id not in topo_order:
            topo_order.append(job_id)

    earliest_finish: dict[str, int] = {}
    best_predecessor: dict[str, str | None] = {}
    for job_id in topo_order:
        preds = predecessors.get(job_id, [])
        if not preds:
            earliest_finish[job_id] = durations[job_id]
            best_predecessor[job_id] = None
        else:
            prev = max(preds, key=lambda p: earliest_finish.get(p, 0))
            earliest_finish[job_id] = earliest_finish.get(prev, 0) + durations[job_id]
            best_predecessor[job_id] = prev

    end_job = max(earliest_finish, key=lambda j: earliest_finish[j])
    total_duration = earliest_finish[end_job]

    # A cycle in the input (which the graph-builder tolerates rather than
    # rejects) can make best_predecessor pointers loop back on themselves —
    # guard reconstruction with a visited set so this never hangs/OOMs.
    path: list[str] = []
    visited: set[str] = set()
    cursor: str | None = end_job
    while cursor is not None and cursor not in visited:
        visited.add(cursor)
        path.append(cursor)
        cursor = best_predecessor.get(cursor)
    path.reverse()

    longest_job_id = max(durations, key=lambda j: durations[j]) if durations else None

    return {
        "critical_path_job_ids": path,
        "total_duration_seconds": total_duration,
        "longest_job_id": longest_job_id,
    }
