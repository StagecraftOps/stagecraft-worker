
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

    if f"{service_path.rstrip('/')}/build.gradle" in repo_tree_paths:
        return "java"
    return None

def resolve_runtime(
    service_path: str | None,
    service_config: dict,
    repo_tree_paths: set[str],
) -> tuple[str | None, str]:
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
