import os

import pytest

from app.analysis.pattern_frequency import find_repeated_patterns

_FIXTURE_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "pace-stagecraft-monorepo")
_WORKFLOWS_DIR = os.path.join(_FIXTURE_ROOT, ".github", "workflows")

pytestmark = pytest.mark.skipif(
    not os.path.isdir(_WORKFLOWS_DIR), reason="pace-stagecraft-monorepo fixture not checked out locally"
)

def test_shared_template_pattern_detected_across_real_per_service_files():
    per_service_files = [f for f in os.listdir(_WORKFLOWS_DIR) if f.startswith("ci-") and f.endswith(".yml")]
    assert len(per_service_files) > 10

    contents = {}
    for filename in per_service_files:
        with open(os.path.join(_WORKFLOWS_DIR, filename), encoding="utf-8") as f:
            contents[filename] = f.read()

    clusters = find_repeated_patterns(contents, min_occurrences=5)
    assert len(clusters) > 0, "expected at least one repeated component pattern across per-service CI files"

    top_cluster = clusters[0]
    assert top_cluster["occurrence_count"] >= 5
    assert any("_template-" in c for c in top_cluster["pattern_signature"]["components"])
