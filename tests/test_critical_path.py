from app.analysis.critical_path import compute_critical_path

def test_linear_chain_sums_durations():
    jobs = [
        {"job_id": "lint", "duration_seconds": 30},
        {"job_id": "test", "duration_seconds": 120},
        {"job_id": "deploy", "duration_seconds": 60},
    ]
    edges = [("lint", "test"), ("test", "deploy")]

    result = compute_critical_path(jobs, edges)
    assert result["total_duration_seconds"] == 210
    assert result["critical_path_job_ids"] == ["lint", "test", "deploy"]
    assert result["longest_job_id"] == "test"

def test_diamond_takes_the_slower_parallel_branch():

    jobs = [
        {"job_id": "lint", "duration_seconds": 10},
        {"job_id": "unit-test", "duration_seconds": 50},
        {"job_id": "race-condition-test", "duration_seconds": 200},
        {"job_id": "security-scan", "duration_seconds": 30},
        {"job_id": "build-docker", "duration_seconds": 40},
    ]
    edges = [
        ("lint", "unit-test"),
        ("lint", "race-condition-test"),
        ("lint", "security-scan"),
        ("unit-test", "build-docker"),
        ("race-condition-test", "build-docker"),
        ("security-scan", "build-docker"),
    ]

    result = compute_critical_path(jobs, edges)

    assert result["total_duration_seconds"] == 250
    assert result["critical_path_job_ids"] == ["lint", "race-condition-test", "build-docker"]
    assert result["longest_job_id"] == "race-condition-test"

def test_independent_jobs_no_edges():
    jobs = [{"job_id": "a", "duration_seconds": 10}, {"job_id": "b", "duration_seconds": 99}]
    result = compute_critical_path(jobs, [])
    assert result["total_duration_seconds"] == 99
    assert result["critical_path_job_ids"] == ["b"]

def test_empty_jobs_returns_zero():
    result = compute_critical_path([], [])
    assert result == {"critical_path_job_ids": [], "total_duration_seconds": 0, "longest_job_id": None}

def test_missing_duration_treated_as_zero():
    jobs = [{"job_id": "a"}, {"job_id": "b", "duration_seconds": 5}]
    result = compute_critical_path(jobs, [("a", "b")])
    assert result["total_duration_seconds"] == 5

def test_cycle_does_not_raise():
    jobs = [{"job_id": "a", "duration_seconds": 10}, {"job_id": "b", "duration_seconds": 20}]
    edges = [("a", "b"), ("b", "a")]
    result = compute_critical_path(jobs, edges)
    assert result["total_duration_seconds"] >= 0
