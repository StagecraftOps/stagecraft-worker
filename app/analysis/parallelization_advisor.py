"""FR-8: structural parallelization-opportunity detection.

Pure computation, no AI — a `needs:` edge that imposes ordering but has no
accompanying `needs.<job>.outputs.<x>` data dependency is a structural
parallelization candidate: the blocked job doesn't actually consume anything
the blocking job produced, so the ordering constraint may be unnecessary.
"""


def find_parallelization_candidates(
    needs_edges: list[tuple[str, str]], needs_output_edges: list[tuple[str, str]]
) -> list[dict]:
    """
    needs_edges: [(blocking_job, blocked_job), ...] from graph_edges where edge_type='needs'
    needs_output_edges: [(blocking_job, blocked_job), ...] where edge_type='needs_output'

    Returns candidates: edges present in needs_edges but absent from
    needs_output_edges — ordering with no observed data dependency.
    """
    output_dependent = set(needs_output_edges)
    candidates = []

    for blocking_job, blocked_job in needs_edges:
        if (blocking_job, blocked_job) in output_dependent:
            continue
        candidates.append({
            "blocking_job": blocking_job,
            "blocked_job": blocked_job,
            "reason": (
                f"'{blocked_job}' needs '{blocking_job}' to complete first, but does not "
                "reference any of its outputs — the ordering constraint may be unnecessary."
            ),
        })

    return candidates
