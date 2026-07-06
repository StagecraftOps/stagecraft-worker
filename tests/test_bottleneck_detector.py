from app.analysis.bottleneck_detector import detect_bottlenecks

def test_job_on_critical_path_exceeding_p90_is_flagged():
    current_jobs = [{"job_name": "test", "duration_seconds": 500}]
    history = {"test": [100, 110, 120, 130, 140, 150, 160, 170, 180, 190]}
    result = detect_bottlenecks(current_jobs, history, critical_path_job_names=["test"])
    assert len(result) == 1
    assert result[0]["job_name"] == "test"
    assert result[0]["excess_seconds"] > 0

def test_job_not_on_critical_path_is_ignored_even_if_slow():
    current_jobs = [{"job_name": "test", "duration_seconds": 999}]
    history = {"test": [100] * 10}
    result = detect_bottlenecks(current_jobs, history, critical_path_job_names=["other-job"])
    assert result == []

def test_job_within_normal_range_is_not_flagged():
    current_jobs = [{"job_name": "test", "duration_seconds": 105}]
    history = {"test": [100, 101, 102, 103, 104, 105, 106, 107, 108, 109]}
    result = detect_bottlenecks(current_jobs, history, critical_path_job_names=["test"])
    assert result == []

def test_insufficient_history_is_not_flagged_as_noise_guard():
    current_jobs = [{"job_name": "test", "duration_seconds": 500}]
    history = {"test": [100, 100]}
    result = detect_bottlenecks(current_jobs, history, critical_path_job_names=["test"])
    assert result == []

def test_results_sorted_by_excess_descending():
    current_jobs = [
        {"job_name": "a", "duration_seconds": 300},
        {"job_name": "b", "duration_seconds": 500},
    ]
    history = {
        "a": [100, 100, 100, 100, 100],
        "b": [100, 100, 100, 100, 100],
    }
    result = detect_bottlenecks(current_jobs, history, critical_path_job_names=["a", "b"])
    assert [f["job_name"] for f in result] == ["b", "a"]
