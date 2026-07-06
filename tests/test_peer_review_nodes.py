import os

os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/x")
os.environ.setdefault("SECRET_KEY", "test-secret-for-worker")

from app.agents.peer_review_nodes import detect_workflow_changes

_DIFF = """diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
index 1111111..2222222 100644
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -10,6 +10,3 @@ jobs:
   build:
     runs-on: ubuntu-latest
-    steps:
-      - uses: ./.github/workflows/_template-security-scan.yml
diff --git a/src/app.py b/src/app.py
index 3333333..4444444 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,3 +1,4 @@
+import os
"""

def test_detects_only_ci_relevant_files():
    state = {"diff": _DIFF, "agent_trace": []}
    result = detect_workflow_changes(state)
    assert result["changed_workflow_files"] == [".github/workflows/ci.yml"]
    assert "src/app.py" not in result["changed_workflow_files"]

def test_no_ci_changes_returns_empty_list():
    diff = "diff --git a/README.md b/README.md\nindex 111..222 100644\n"
    state = {"diff": diff, "agent_trace": []}
    result = detect_workflow_changes(state)
    assert result["changed_workflow_files"] == []

def test_appends_to_agent_trace_without_mutating_original():
    state = {"diff": _DIFF, "agent_trace": ["prior step"]}
    result = detect_workflow_changes(state)
    assert result["agent_trace"][0] == "prior step"
    assert len(result["agent_trace"]) == 2

def test_composite_action_changes_also_detected():
    diff = "diff --git a/.github/actions/node-ci/action.yml b/.github/actions/node-ci/action.yml\nindex 1..2 100644\n"
    state = {"diff": diff, "agent_trace": []}
    result = detect_workflow_changes(state)
    assert result["changed_workflow_files"] == [".github/actions/node-ci/action.yml"]
