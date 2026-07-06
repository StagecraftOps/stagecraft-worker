from app.analysis.orchestrator_parser import parse_orchestrator

def test_layers_computed_from_depends_on():
    content = """
services:
  infra/api-gateway: {}
  auth/auth-service: {}
  auth/token-service:
    depends_on: ["auth/auth-service"]
  backend/zestimate-service:
    depends_on: ["auth/token-service", "infra/api-gateway"]
"""
    nodes, edges = parse_orchestrator(content)
    layers = {n["display_name"]: n["metadata"]["layer"] for n in nodes}

    assert layers["infra/api-gateway"] == 0
    assert layers["auth/auth-service"] == 0
    assert layers["auth/token-service"] == 1
    assert layers["backend/zestimate-service"] == 2

    dep_edges = {(e["source_key"], e["target_key"]) for e in edges}
    assert ("service::auth/auth-service", "service::auth/token-service") in dep_edges
    assert ("service::auth/token-service", "service::backend/zestimate-service") in dep_edges
    assert ("service::infra/api-gateway", "service::backend/zestimate-service") in dep_edges

def test_circular_dependency_is_flattened_not_raised():
    content = """
services:
  a:
    depends_on: ["b"]
  b:
    depends_on: ["a"]
"""
    nodes, edges = parse_orchestrator(content)

    assert {n["display_name"] for n in nodes} == {"a", "b"}
    assert len(edges) == 2

def test_missing_services_key_returns_empty():
    nodes, edges = parse_orchestrator("not_services: {}\n")
    assert nodes == []
    assert edges == []
