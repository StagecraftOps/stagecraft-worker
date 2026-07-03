"""FR-4: detects step/component patterns repeated across many workflows in
an org — candidates worth extracting into a shared reusable template
component. Pure computation (signature hashing + counting), no AI.
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
            "pattern_signature": {"components": list(signature)},
            "occurrence_count": len(files),
            "example_workflow_files": sorted(files)[:5],
        })

    clusters.sort(key=lambda c: c["occurrence_count"], reverse=True)
    return clusters
