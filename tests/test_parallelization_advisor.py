from app.analysis.parallelization_advisor import find_parallelization_candidates

def test_needs_edge_without_output_dependency_is_a_candidate():
    needs_edges = [("lint", "security-scan")]
    needs_output_edges = []
    result = find_parallelization_candidates(needs_edges, needs_output_edges)
    assert len(result) == 1
    assert result[0]["blocking_job"] == "lint"
    assert result[0]["blocked_job"] == "security-scan"

def test_needs_edge_with_output_dependency_is_not_a_candidate():
    needs_edges = [("build", "deploy")]
    needs_output_edges = [("build", "deploy")]
    result = find_parallelization_candidates(needs_edges, needs_output_edges)
    assert result == []

def test_mixed_edges_only_flags_the_ones_without_output_dependency():
    needs_edges = [("lint", "unit-test"), ("build", "deploy")]
    needs_output_edges = [("build", "deploy")]
    result = find_parallelization_candidates(needs_edges, needs_output_edges)
    assert len(result) == 1
    assert result[0]["blocking_job"] == "lint"

def test_no_needs_edges_returns_empty():
    assert find_parallelization_candidates([], []) == []
