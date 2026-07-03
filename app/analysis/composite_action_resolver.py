"""Resolves which composite action actually fires for a runtime-gated step.

Patterns like pace-stagecraft-monorepo's service-ci.yml pick a composite
action (node-ci/python-ci/go-ci/java-ci) via a step `if:` condition evaluated
against a runtime string that's only known by reading service-config.json or
the target service's own files (package.json, requirements.txt, go.mod,
pom.xml/build.gradle) — not statically knowable from the workflow YAML alone.
"""

_RUNTIME_MARKERS = {
    "node": "package.json",
    "python": "requirements.txt",
    "go": "go.mod",
    "java": "pom.xml",
}


def _detect_runtime_from_tree(service_path: str, repo_tree_paths: set[str]) -> str | None:
    for runtime, marker in _RUNTIME_MARKERS.items():
        if f"{service_path.rstrip('/')}/{marker}" in repo_tree_paths:
            return runtime
    # build.gradle is Java's alternate marker
    if f"{service_path.rstrip('/')}/build.gradle" in repo_tree_paths:
        return "java"
    return None


def resolve_runtime(
    service_path: str | None,
    service_config: dict,
    repo_tree_paths: set[str],
) -> tuple[str | None, str]:
    """Return (runtime, confidence). runtime is None if it can't be resolved at all."""
    if not service_path:
        return None, "ambiguous"

    service_folder = service_path.rstrip("/").rsplit("/", 1)[-1]
    override = service_config.get(service_folder)
    if isinstance(override, dict) and override.get("runtime"):
        return override["runtime"], "certain"

    detected = _detect_runtime_from_tree(service_path, repo_tree_paths)
    if detected:
        return detected, "certain"

    return None, "ambiguous"
