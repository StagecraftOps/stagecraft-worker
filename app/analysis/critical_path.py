
def compute_critical_path(
    jobs: list[dict], needs_edges: list[tuple[str, str]]
) -> dict:
    if not jobs:
        return {"critical_path_job_ids": [], "total_duration_seconds": 0, "longest_job_id": None}

    durations = {j["job_id"]: j.get("duration_seconds") or 0 for j in jobs}
    predecessors: dict[str, list[str]] = {job_id: [] for job_id in durations}
    successors: dict[str, list[str]] = {job_id: [] for job_id in durations}

    for source, target in needs_edges:
        if source in durations and target in durations:
            predecessors[target].append(source)
            successors[source].append(target)

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
