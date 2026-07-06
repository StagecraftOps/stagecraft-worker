import re

import yaml

_VERSION_SPLIT = re.compile(r"^(.*)@([^@]+)$")

def _extract_components(workflow_yaml: str) -> dict[str, str | None]:
    try:
        doc = yaml.safe_load(workflow_yaml)
    except yaml.YAMLError:
        return {}
    if not isinstance(doc, dict):
        return {}

    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return {}

    components: dict[str, str | None] = {}
    for job_def in jobs.values():
        if not isinstance(job_def, dict):
            continue

        job_uses = job_def.get("uses")
        if isinstance(job_uses, str):
            base, version = _split_version(job_uses)
            components[base] = version

        for step in job_def.get("steps") or []:
            if not isinstance(step, dict):
                continue
            step_uses = step.get("uses")
            if isinstance(step_uses, str):
                base, version = _split_version(step_uses)
                components[base] = version

    return components

def _split_version(uses: str) -> tuple[str, str | None]:
    match = _VERSION_SPLIT.match(uses)
    if match:
        return match.group(1), match.group(2)
    return uses, None

def diff_workflow_against_template(workflow_yaml: str, template_yaml: str) -> dict:
    template_components = _extract_components(template_yaml)
    workflow_components = _extract_components(workflow_yaml)

    missing = sorted(set(template_components) - set(workflow_components))
    extra = sorted(set(workflow_components) - set(template_components))

    version_drift = []
    for name in sorted(set(template_components) & set(workflow_components)):
        template_version = template_components[name]
        workflow_version = workflow_components[name]
        if template_version and workflow_version and template_version != workflow_version:
            version_drift.append({
                "component": name,
                "template_version": template_version,
                "workflow_version": workflow_version,
            })

    if not template_components:
        adoption_score = 100
    else:
        present = len(template_components) - len(missing)
        adoption_score = round(100 * present / len(template_components))

    return {
        "missing_components": missing,
        "extra_components": extra,
        "version_drift": version_drift,
        "adoption_score": adoption_score,
    }

def narrate_diff(diff: dict, workflow_file: str, template_name: str) -> str:
    if not diff["missing_components"] and not diff["version_drift"]:
        return f"{workflow_file} fully adopts the '{template_name}' template."
    parts = []
    if diff["missing_components"]:
        parts.append(f"missing {', '.join(diff['missing_components'])}")
    if diff["version_drift"]:
        drifted = ", ".join(d["component"] for d in diff["version_drift"])
        parts.append(f"version drift on {drifted}")
    return f"{workflow_file} is {diff['adoption_score']}% aligned with '{template_name}': " + "; ".join(parts) + "."
