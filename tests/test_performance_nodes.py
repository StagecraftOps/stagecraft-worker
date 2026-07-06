import os

os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/x")
os.environ.setdefault("SECRET_KEY", "test-secret-for-worker")

from app.agents.performance_nodes import simulate_savings

def test_no_draft_yaml_means_baseline_equals_simulated():
    state = {
        "job_durations": {"lint": 10, "test": 100, "deploy": 20},
        "needs_edges": [["lint", "test"], ["test", "deploy"]],
        "draft_future_yaml": None,
        "workflow_file": "ci.yml",
        "agent_trace": [],
    }
    result = simulate_savings(state)
    assert result["baseline_critical_path_seconds"] == 130
    assert result["simulated_critical_path_seconds"] == 130

def test_draft_yaml_removing_a_needs_edge_reduces_simulated_duration():
    state = {
        "job_durations": {"lint": 10, "slow-test": 100, "deploy": 20},
        "needs_edges": [["lint", "slow-test"], ["slow-test", "deploy"]],
        "draft_future_yaml": """
name: CI
on: push
jobs:
  lint:
    runs-on: ubuntu-latest
    steps: []
  slow-test:
    runs-on: ubuntu-latest
    steps: []
  deploy:
    runs-on: ubuntu-latest
    needs: slow-test
    steps: []
""",
        "workflow_file": "ci.yml",
        "agent_trace": [],
    }
    result = simulate_savings(state)

    assert result["baseline_critical_path_seconds"] == 130

    assert result["simulated_critical_path_seconds"] == 120
