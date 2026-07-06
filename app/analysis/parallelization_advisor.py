
def find_parallelization_candidates(
    needs_edges: list[tuple[str, str]], needs_output_edges: list[tuple[str, str]]
) -> list[dict]:
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
