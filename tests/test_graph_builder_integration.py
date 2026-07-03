"""Integration test: run the parser against the real pace-stagecraft-monorepo
fixture (110 real workflow files, 3 overlapping CI patterns) instead of a
synthetic sample, per the requirement that graph-building be validated
against real data. Skipped if the sibling fixture repo isn't checked out
locally (e.g. in a CI job that only has this repo).
"""
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
    assert len(nodes) > 50  # orchestrator.yaml documents 70+ real service entries
    assert len(edges) > 0

    layers = {n["metadata"]["layer"] for n in nodes}
    assert 0 in layers  # every real orchestrator.yaml has at least one layer-0 (no-dep) service
    assert len(layers) > 1  # and at least one dependent layer above it


def test_real_service_ci_yml_produces_ambiguous_composite_edges():
    path = os.path.join(_WORKFLOWS_DIR, "service-ci.yml")
    if not os.path.isfile(path):
        pytest.skip("service-ci.yml not present in fixture")
    with open(path, encoding="utf-8") as f:
        content = f.read()

    nodes, edges = parse_workflow(".github/workflows/service-ci.yml", content)
    composite_edges = [e for e in edges if e["edge_type"] == "uses_composite"]
    # Each runtime branch (node/python/go/java) is gated by a runtime `if:`
    # unresolved without service-config.json / file auto-detection.
    assert len(composite_edges) >= 4
    assert all(e["confidence"] == "ambiguous" for e in composite_edges)


def test_real_ci_yml_orchestrator_workflow_parses_without_error():
    path = os.path.join(_WORKFLOWS_DIR, "ci.yml")
    if not os.path.isfile(path):
        pytest.skip("ci.yml not present in fixture")
    with open(path, encoding="utf-8") as f:
        content = f.read()

    # Must not raise even though this file's `detect-changes` job hand-rolls
    # its own bash+jq orchestrator.yaml parsing and Kahn's algorithm — our
    # parser only needs the job/uses graph, not to execute that logic.
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

    # The documented pattern is lint -> {unit-test, race-condition-test,
    # security-scan} -> build-docker -> integration-test -> deploy-staging,
    # with 3+ reusable-workflow template calls per file.
    assert total_needs_edges > 20
    assert total_reusable_edges > 20


def test_real_dispatch_detector_finds_no_false_positives_on_ordinary_workflow():
    path = os.path.join(_WORKFLOWS_DIR, "ci.yml")
    if not os.path.isfile(path):
        pytest.skip("ci.yml not present in fixture")
    with open(path, encoding="utf-8") as f:
        content = f.read()

    # ci.yml doesn't repository_dispatch anywhere; parent.yaml does. This
    # just confirms the heuristic doesn't fire on ordinary curl-free jobs.
    nodes, edges = find_dispatch_edges(".github/workflows/ci.yml", content)
    assert nodes == []
    assert edges == []
