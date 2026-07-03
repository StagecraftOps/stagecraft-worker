"""Tests for app/analysis/workflow_parser.py — pure YAML parsing, no DB/network."""
from app.analysis.workflow_parser import parse_workflow


def test_job_needs_edge():
    content = """
name: CI
on: push
jobs:
  lint:
    runs-on: ubuntu-latest
    steps: []
  test:
    runs-on: ubuntu-latest
    needs: lint
    steps: []
"""
    nodes, edges = parse_workflow(".github/workflows/ci.yml", content)
    job_nodes = {n["external_key"] for n in nodes if n["node_type"] == "job"}
    assert "job::.github/workflows/ci.yml::lint" in job_nodes
    assert "job::.github/workflows/ci.yml::test" in job_nodes

    needs_edges = [e for e in edges if e["edge_type"] == "needs"]
    assert len(needs_edges) == 1
    assert needs_edges[0]["source_key"] == "job::.github/workflows/ci.yml::lint"
    assert needs_edges[0]["target_key"] == "job::.github/workflows/ci.yml::test"


def test_reusable_workflow_call_and_matrix_fanout():
    content = """
name: Domain CI
on:
  workflow_call:
    inputs:
      domain: {required: true, type: string}
jobs:
  run-service:
    strategy:
      matrix:
        service: ["a", "b"]
    uses: ./.github/workflows/service-ci.yml
    with:
      domain: backend
"""
    nodes, edges = parse_workflow(".github/workflows/domain-ci.yml", content)
    reusable = [n for n in nodes if n["node_type"] == "reusable_workflow"]
    assert reusable[0]["display_name"] == "./.github/workflows/service-ci.yml"

    fanout_edges = [e for e in edges if e["edge_type"] == "matrix_fanout"]
    assert len(fanout_edges) == 1
    assert fanout_edges[0]["metadata"]["matrix"] == {"service": ["a", "b"]}


def test_composite_action_local_uses_edge():
    content = """
name: Service CI
on: workflow_call
jobs:
  build-and-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Run Node CI
        if: steps.config.outputs.runtime == 'node'
        uses: ./.github/actions/node-ci
        with:
          path: services/backend/listing-service
"""
    nodes, edges = parse_workflow(".github/workflows/service-ci.yml", content)
    composite_nodes = [n for n in nodes if n["node_type"] == "composite_action"]
    assert len(composite_nodes) == 1
    assert composite_nodes[0]["display_name"] == "./.github/actions/node-ci"

    # Marketplace action (actions/checkout@v4) must NOT be graphed as a node.
    assert all(n["display_name"] != "actions/checkout@v4" for n in nodes)

    composite_edges = [e for e in edges if e["edge_type"] == "uses_composite"]
    assert len(composite_edges) == 1
    # Gated by a runtime-only `if:` — unresolved at parse time, so ambiguous.
    assert composite_edges[0]["confidence"] == "ambiguous"


def test_needs_output_data_dependency():
    content = """
name: CI
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    outputs:
      image_uri: ${{ steps.build.outputs.digest }}
    steps: []
  deploy:
    runs-on: ubuntu-latest
    needs: build
    steps:
      - run: echo "deploying ${{ needs.build.outputs.image_uri }}"
"""
    nodes, edges = parse_workflow(".github/workflows/ci.yml", content)
    output_edges = [e for e in edges if e["edge_type"] == "needs_output"]
    assert len(output_edges) == 1
    assert output_edges[0]["source_key"] == "job::.github/workflows/ci.yml::build"
    assert output_edges[0]["target_key"] == "job::.github/workflows/ci.yml::deploy"


def test_unparsable_yaml_returns_empty():
    nodes, edges = parse_workflow("bad.yml", "not: valid: yaml: [")
    assert nodes == []
    assert edges == []


def test_non_workflow_yaml_without_jobs_returns_empty():
    nodes, edges = parse_workflow("random.yml", "just_a_key: value\n")
    assert nodes == []
    assert edges == []
