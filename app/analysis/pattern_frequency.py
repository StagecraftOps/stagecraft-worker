import hashlib
import re

import yaml

_VERSION_SPLIT = re.compile(r"^(.*)@([^@]+)$")
_MIN_COMPONENTS_FOR_SIGNAL = 2

def classify_pattern_type(signature: tuple[str, ...]) -> str:
    if len(signature) != 1:
        return "JOB"
    component = signature[0]
    if ".github/workflows/" in component or component.endswith((".yml", ".yaml")):
        return "WORKFLOW"
    return "ACTION"

def _strip_version(uses: str) -> str:
    match = _VERSION_SPLIT.match(uses)
    return match.group(1) if match else uses

def _job_signatures(path: str, content: str) -> list[tuple[str, tuple[str, ...]]]:
    try:
        doc = yaml.safe_load(content)
    except yaml.YAMLError:
        return []
    if not isinstance(doc, dict):
        return []

    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return []

    signatures = []
    for job_id, job_def in jobs.items():
        if not isinstance(job_def, dict):
            continue

        job_uses = job_def.get("uses")
        if isinstance(job_uses, str):
            signatures.append((f"{path}::{job_id}", (_strip_version(job_uses),)))
            continue

        components: set[str] = set()
        for step in job_def.get("steps") or []:
            if isinstance(step, dict) and isinstance(step.get("uses"), str):
                components.add(_strip_version(step["uses"]))

        if len(components) >= _MIN_COMPONENTS_FOR_SIGNAL:
            signatures.append((f"{path}::{job_id}", tuple(sorted(components))))

    return signatures

def find_repeated_patterns(
    workflow_contents: dict[str, str], min_occurrences: int = 3
) -> list[dict]:
    signature_to_files: dict[tuple[str, ...], set[str]] = {}

    for path, content in workflow_contents.items():
        for _job_key, signature in _job_signatures(path, content):
            signature_to_files.setdefault(signature, set()).add(path)

    clusters = []
    for signature, files in signature_to_files.items():
        if len(files) < min_occurrences:
            continue
        pattern_hash = hashlib.sha256("|".join(signature).encode()).hexdigest()
        clusters.append({
            "pattern_hash": pattern_hash,
            "pattern_signature": {
                "components": list(signature),
                "match_type": "exact",
                "candidate_type": classify_pattern_type(signature),
            },
            "occurrence_count": len(files),
            "example_workflow_files": sorted(files)[:5],
        })

    clusters.sort(key=lambda c: c["occurrence_count"], reverse=True)
    return clusters

def _jaccard(a: tuple[str, ...], b: tuple[str, ...]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)

def find_near_miss_groups(
    workflow_contents: dict[str, str],
    exact_clusters: list[dict],
    min_occurrences: int = 3,
    similarity_threshold: float = 0.6,
) -> list[list[dict]]:
    exact_signatures = {tuple(c["pattern_signature"]["components"]) for c in exact_clusters}

    candidate_jobs: list[tuple[str, tuple[str, ...]]] = []
    for path, content in workflow_contents.items():
        for job_key, signature in _job_signatures(path, content):
            if signature in exact_signatures:
                continue
            candidate_jobs.append((job_key, signature))

    groups: list[list[tuple[str, tuple[str, ...]]]] = []
    for job_key, signature in candidate_jobs:
        for group in groups:
            if any(_jaccard(signature, other_sig) >= similarity_threshold for _, other_sig in group):
                group.append((job_key, signature))
                break
        else:
            groups.append([(job_key, signature)])

    return [
        [{"job_key": k, "components": list(sig)} for k, sig in group]
        for group in groups
        if len(group) >= min_occurrences
    ]
