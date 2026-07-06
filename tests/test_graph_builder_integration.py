import os

import pytest

from app.analysis.dispatch_detector import find_dispatch_edges
from app.analysis.orchestrator_parser import parse_orchestrator
from app.analysis.workflow_parser import parse_workflow

_FIXTURE_ROOT = os.path.join(
    os.path.dirname(__file__), "..", "..", "pace-stagecraft-monorepo"
)
_ORCHESTRATOR_PATH = os.path.join(_FIXTURE_ROOT, "orchestrator.yaml")
_WORKFLOWS_DIR = os.path.join(_FIXTURE_ROOT, ".github", "workflows")

pytestmark = pytest.mark.skipif(
    not os.path.isdir(_FIXTURE_ROOT), reason="pace-stagecraft-monorepo fixture not checked out locally"
)

def test_real_orchestrator_yaml_parses_into_layered_services():
    with open(_ORCHESTRATOR_PATH, encoding="utf-8") as f:
        content = f.read()

    nodes, edges = parse_orchestrator(content)
    assert len(nodes) > 50
    assert len(edges) > 0

    layers = {n["metadata"]["layer"] for n in nodes}
    assert 0 in layers
    assert len(layers) > 1

def test_real_service_ci_yml_produces_ambiguous_composite_edges():
    path = os.path.join(_WORKFLOWS_DIR, "service-ci.yml")
    if not os.path.isfile(path):
        pytest.skip("service-ci.yml not present in fixture")
    with open(path, encoding="utf-8") as f:
        content = f.read()

    nodes, edges = parse_workflow(".github/workflows/service-ci.yml", content)
    composite_edges = [e for e in edges if e["edge_type"] == "uses_composite"]

    assert len(composite_edges) >= 4
    assert all(e["confidence"] == "ambiguous" for e in composite_edges)

def test_real_ci_yml_orchestrator_workflow_parses_without_error():
    path = os.path.join(_WORKFLOWS_DIR, "ci.yml")
    if not os.path.isfile(path):
        pytest.skip("ci.yml not present in fixture")
    with open(path, encoding="utf-8") as f:
        content = f.read()

    nodes, edges = parse_workflow(".github/workflows/ci.yml", content)
    assert isinstance(nodes, list)
    assert isinstance(edges, list)

def test_real_per_service_ci_files_produce_needs_chains():
    if not os.path.isdir(_WORKFLOWS_DIR):
        pytest.skip("workflows dir not present in fixture")

    per_service_files = [
        f for f in os.listdir(_WORKFLOWS_DIR)
        if f.startswith("ci-") and f.endswith(".yml")
    ]
    assert len(per_service_files) > 10

    total_needs_edges = 0
    total_reusable_edges = 0
    for filename in per_service_files[:20]:
        path = os.path.join(_WORKFLOWS_DIR, filename)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        nodes, edges = parse_workflow(f".github/workflows/{filename}", content)
        total_needs_edges += len([e for e in edges if e["edge_type"] == "needs"])
        total_reusable_edges += len(
            [e for e in edges if e["edge_type"] in ("uses_reusable", "matrix_fanout")]
        )

    assert total_needs_edges > 20
    assert total_reusable_edges > 20

def test_real_dispatch_detector_finds_no_false_positives_on_ordinary_workflow():
    path = os.path.join(_WORKFLOWS_DIR, "ci.yml")
    if not os.path.isfile(path):
        pytest.skip("ci.yml not present in fixture")
    with open(path, encoding="utf-8") as f:
        content = f.read()

    nodes, edges = find_dispatch_edges(".github/workflows/ci.yml", content)
    assert nodes == []
    assert edges == []
