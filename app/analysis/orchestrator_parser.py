"""Parses an orchestrator.yaml-style service dependency DAG.

Format: services: { "domain/service-name": { depends_on: ["domain/other", ...] } }
This is a separate, non-GHA dependency graph the target repo's own CI (e.g.
pace-stagecraft-monorepo's ci.yml) uses to compute build-order layers via
Kahn's algorithm. We parse the file directly as YAML rather than replaying
that repo's bash+regex parser, and compute layers the same way: services with
no unresolved dependency go in the next layer; any remaining cycle is
flattened into the current layer rather than raising, matching the target
repo's own documented fallback behavior.
"""
import yaml


def parse_orchestrator(content: str) -> tuple[list[dict], list[dict]]:
    """Return (nodes, edges) for a repo's orchestrator.yaml. ([], []) if unparsable."""
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
    """Kahn's algorithm; unresolved cycles are flattened into the current layer."""
    remaining = set(depends_on.keys())
    layers: dict[str, int] = {}
    current_layer = 0

    while remaining:
        ready = {
            name for name in remaining
            if all(dep not in remaining for dep in depends_on.get(name, []))
        }
        if not ready:
            # Circular dependency among what's left — flatten it into this layer.
            ready = set(remaining)
        for name in ready:
            layers[name] = current_layer
        remaining -= ready
        current_layer += 1

    return layers
