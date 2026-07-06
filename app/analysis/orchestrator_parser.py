import yaml

def parse_orchestrator(content: str) -> tuple[list[dict], list[dict]]:
    try:
        doc = yaml.safe_load(content)
    except yaml.YAMLError:
        return [], []
    if not isinstance(doc, dict):
        return [], []

    services = doc.get("services")
    if not isinstance(services, dict):
        return [], []

    depends_on: dict[str, list[str]] = {}
    for name, spec in services.items():
        deps = (spec or {}).get("depends_on") or [] if isinstance(spec, dict) else []
        depends_on[name] = [d for d in deps if isinstance(d, str)]

    layers = _compute_layers(depends_on)

    nodes = [
        {
            "node_type": "service",
            "external_key": f"service::{name}",
            "display_name": name,
            "workflow_file": None,
            "job_id": None,
            "metadata": {"layer": layers.get(name, 0)},
        }
        for name in depends_on
    ]
    edges = [
        {
            "source_key": f"service::{dep}",
            "target_key": f"service::{name}",
            "edge_type": "orchestrator_service_dep",
            "confidence": "certain",
            "metadata": None,
        }
        for name, deps in depends_on.items()
        for dep in deps
        if dep in depends_on
    ]
    return nodes, edges

def _compute_layers(depends_on: dict[str, list[str]]) -> dict[str, int]:
    remaining = set(depends_on.keys())
    layers: dict[str, int] = {}
    current_layer = 0

    while remaining:
        ready = {
            name for name in remaining
            if all(dep not in remaining for dep in depends_on.get(name, []))
        }
        if not ready:

            ready = set(remaining)
        for name in ready:
            layers[name] = current_layer
        remaining -= ready
        current_layer += 1

    return layers
