
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
    critical_set = set(critical_path_job_names)
    findings = []

    for job in current_jobs:
        name = job["job_name"]
        duration = job.get("duration_seconds") or 0
        if name not in critical_set:
            continue

        history = historical_durations_by_name.get(name, [])
        if len(history) < 3:
            continue

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
