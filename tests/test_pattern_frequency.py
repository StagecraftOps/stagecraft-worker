"""Tests for app/analysis/pattern_frequency.py — pure signature-hashing, no AI."""
from app.analysis.pattern_frequency import find_repeated_patterns

_SHARED_PATTERN = """
name: {name}
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: ./.github/workflows/_template-security-scan.yml
      - uses: ./.github/workflows/_template-docker-build.yml
"""


def test_pattern_repeated_across_three_workflows_is_detected():
    contents = {
        f"ci-{name}.yml": _SHARED_PATTERN.format(name=name)
        for name in ["a", "b", "c"]
    }
    clusters = find_repeated_patterns(contents, min_occurrences=3)
    assert len(clusters) == 1
    assert clusters[0]["occurrence_count"] == 3
    assert set(clusters[0]["pattern_signature"]["components"]) == {
        "./.github/workflows/_template-security-scan.yml",
        "./.github/workflows/_template-docker-build.yml",
    }


def test_pattern_below_threshold_is_not_reported():
    contents = {
        f"ci-{name}.yml": _SHARED_PATTERN.format(name=name)
        for name in ["a", "b"]
    }
    clusters = find_repeated_patterns(contents, min_occurrences=3)
    assert clusters == []


def test_trivial_single_component_jobs_are_ignored():
    contents = {
        f"ci-{name}.yml": f"""
name: {name}
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
"""
        for name in ["a", "b", "c"]
    }
    # A single-component signature never crosses _MIN_COMPONENTS_FOR_SIGNAL, so
    # it must not be reported as a "pattern" even though it's repeated 3x.
    clusters = find_repeated_patterns(contents, min_occurrences=3)
    assert clusters == []


def test_unrelated_workflows_produce_no_clusters():
    contents = {
        "a.yml": "name: A\non: push\njobs:\n  x:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: foo/bar@v1\n      - uses: foo/baz@v1\n",
        "b.yml": "name: B\non: push\njobs:\n  y:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: qux/quux@v1\n      - uses: qux/corge@v1\n",
    }
    clusters = find_repeated_patterns(contents, min_occurrences=2)
    assert clusters == []


def test_results_sorted_by_occurrence_count_descending():
    contents = {}
    for i in range(4):
        contents[f"common-{i}.yml"] = _SHARED_PATTERN.format(name=f"common-{i}")
    for i in range(3):
        contents[f"rare-{i}.yml"] = f"""
name: rare-{i}
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: foo/action-a@v1
      - uses: foo/action-b@v1
"""
    clusters = find_repeated_patterns(contents, min_occurrences=3)
    assert len(clusters) == 2
    assert clusters[0]["occurrence_count"] == 4
    assert clusters[1]["occurrence_count"] == 3
