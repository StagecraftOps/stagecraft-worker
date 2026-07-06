"""Tests for app/analysis/pattern_frequency.py — pure signature-hashing, no AI."""
from app.analysis.pattern_frequency import find_near_miss_groups, find_repeated_patterns

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


def test_exact_clusters_are_tagged_as_exact_match_type():
    contents = {
        f"ci-{name}.yml": _SHARED_PATTERN.format(name=name)
        for name in ["a", "b", "c"]
    }
    clusters = find_repeated_patterns(contents, min_occurrences=3)
    assert clusters[0]["pattern_signature"]["match_type"] == "exact"


def test_near_miss_groups_similar_but_not_identical_jobs():
    """Three jobs share 2 of 3 components (missed by exact hashing since the
    third component differs) -- similar enough (Jaccard 2/4 = 0.5 by default
    threshold 0.6 would NOT match; use a workflow set that clears 0.6)."""
    contents = {
        "a.yml": "name: A\non: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: ./.github/workflows/_template-security-scan.yml\n      - uses: ./.github/workflows/_template-docker-build.yml\n",
        "b.yml": "name: B\non: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: ./.github/workflows/_template-security-scan.yml\n      - uses: ./.github/workflows/_template-docker-build.yml\n",
        "c.yml": "name: C\non: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: ./.github/workflows/_template-security-scan.yml\n      - uses: ./.github/workflows/_template-docker-build.yml\n      - uses: ./.github/workflows/_template-extra-lint.yml\n",
    }
    # a and b are byte-identical (exact cluster of 2, below min_occurrences=3
    # so not reported by find_repeated_patterns) -- c has one extra
    # component, so its signature never exact-matches a/b's. All three
    # should end up in one near-miss group (Jaccard(a,c) = 2/3 >= 0.6).
    exact_clusters = find_repeated_patterns(contents, min_occurrences=3)
    assert exact_clusters == []  # nothing reaches min_occurrences=3 exactly

    groups = find_near_miss_groups(contents, exact_clusters, min_occurrences=3, similarity_threshold=0.6)
    assert len(groups) == 1
    assert len(groups[0]) == 3
    job_keys = {j["job_key"] for j in groups[0]}
    assert job_keys == {"a.yml::build", "b.yml::build", "c.yml::build"}


def test_near_miss_groups_exclude_jobs_already_exactly_clustered():
    """A job whose signature IS an exact cluster shouldn't also show up as a
    'near miss' candidate -- exact hashing already proved the match."""
    contents = {
        f"ci-{name}.yml": _SHARED_PATTERN.format(name=name)
        for name in ["a", "b", "c"]
    }
    exact_clusters = find_repeated_patterns(contents, min_occurrences=3)
    assert len(exact_clusters) == 1

    groups = find_near_miss_groups(contents, exact_clusters, min_occurrences=3)
    assert groups == []


def test_near_miss_groups_below_min_occurrences_are_dropped():
    contents = {
        "a.yml": "name: A\non: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: ./.github/workflows/_template-security-scan.yml\n      - uses: ./.github/workflows/_template-docker-build.yml\n",
        "b.yml": "name: B\non: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: ./.github/workflows/_template-security-scan.yml\n      - uses: ./.github/workflows/_template-docker-build.yml\n      - uses: ./.github/workflows/_template-extra-lint.yml\n",
    }
    groups = find_near_miss_groups(contents, [], min_occurrences=3, similarity_threshold=0.6)
    assert groups == []
