"""FR-7: statistical outlier detection on critical-path job durations.

Pure computation, no AI — flags a job as a bottleneck when it's on the
critical path AND its duration is a statistical outlier (p90+) compared to
the same-named job's historical durations across the org.
"""


def _percentile(values: list[int], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * pct
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = index - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def detect_bottlenecks(
    current_jobs: list[dict],
    historical_durations_by_name: dict[str, list[int]],
    critical_path_job_names: list[str],
    percentile: float = 0.9,
) -> list[dict]:
    """
    current_jobs: [{"job_name": str, "duration_seconds": int}, ...] for one run
    historical_durations_by_name: {job_name: [duration_seconds, ...]} across the org
    critical_path_job_names: job names on this run's critical path

    Returns bottleneck findings for jobs that are both on the critical path
    and a p90+ outlier relative to their own historical distribution.
    """
    critical_set = set(critical_path_job_names)
    findings = []

    for job in current_jobs:
        name = job["job_name"]
        duration = job.get("duration_seconds") or 0
        if name not in critical_set:
            continue

        history = historical_durations_by_name.get(name, [])
        if len(history) < 3:
            continue  # not enough history to call this an outlier, not noise

        baseline_p90 = _percentile(history, percentile)
        if duration > baseline_p90:
            findings.append({
                "job_name": name,
                "duration_seconds": duration,
                "baseline_p90_seconds": round(baseline_p90),
                "excess_seconds": round(duration - baseline_p90),
            })

    findings.sort(key=lambda f: f["excess_seconds"], reverse=True)
    return findings
