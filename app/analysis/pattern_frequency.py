"""FR-4: detects step/component patterns repeated across many workflows in
an org — candidates worth extracting into a shared reusable template
component. find_repeated_patterns below is pure computation (signature
hashing + counting), no AI. find_near_miss_groups is also pure computation
(similarity grouping) -- the LLM judgment itself lives in
BedrockRemediationClient.judge_pattern_cluster, called from
app.tasks.standardization on the groups this returns.
"""
import hashlib
import re

import yaml

_VERSION_SPLIT = re.compile(r"^(.*)@([^@]+)$")
_MIN_COMPONENTS_FOR_SIGNAL = 2  # ignore trivial 0-1 component jobs — too noisy to be a "pattern"


def _strip_version(uses: str) -> str:
    match = _VERSION_SPLIT.match(uses)
    return match.group(1) if match else uses


def _job_signatures(path: str, content: str) -> list[tuple[str, tuple[str, ...]]]:
    """Return [(f"{path}::{job_id}", sorted_component_names), ...] for one workflow file.

    A job-level `uses:` (the entire job delegates to one reusable workflow) is
    always significant on its own — that's precisely the "reusable template
    component" signal FR-4 looks for, not noise. Step-level-only signatures
    (ordinary marketplace actions like actions/checkout) require >= 2 distinct
    components to avoid flagging a single ubiquitous action as a "pattern".
    """
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
    """workflow_contents: {workflow_file_path: yaml_content}.

    Returns pattern-cluster dicts for any component signature that recurs in
    at least `min_occurrences` distinct workflow files.
    """
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
            "pattern_signature": {"components": list(signature), "match_type": "exact"},
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
    """Group jobs whose signatures are similar-but-not-identical -- missed by
    find_repeated_patterns' exact-hash matching -- into candidate groups for
    LLM judgment (BedrockRemediationClient.judge_pattern_cluster).

    Only jobs NOT already part of an exact cluster are considered (no point
    asking the LLM to confirm what exact hashing already proved). Grouping is
    greedy single-linkage: a job joins the first existing group any of whose
    members it's similar enough to, which is intentionally simple rather than
    a proper clustering algorithm -- at this scale (a few hundred jobs per
    analysis run) the O(n^2) pairwise comparison this implies is cheap, and
    the LLM call downstream is the actual judgment step, not this grouping.
    """
    exact_signatures = {tuple(c["pattern_signature"]["components"]) for c in exact_clusters}

    candidate_jobs: list[tuple[str, tuple[str, ...]]] = []
    for path, content in workflow_contents.items():
        for job_key, signature in _job_signatures(path, content):
            if signature in exact_signatures:
                continue  # already exactly clustered -- not a "near miss"
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
