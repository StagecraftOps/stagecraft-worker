from app.analysis.graph_builder import _identity_key, build_graph_data

_CALLER = """
name: Caller CI
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    uses: ./.github/workflows/_template-notify-slack.yml
"""

_CALLEE = """
name: Notify Slack
on: workflow_call
jobs:
  notify:
    runs-on: ubuntu-latest
    steps:
      - run: echo notify
"""

class _FakeGitHub:
    def __init__(self, files_in_order):
        self._files_in_order = files_in_order

    def get_repo_tree(self, owner, repo, ref):
        return [{"path": p, "type": "blob"} for p, _ in self._files_in_order]

    def get_file_content(self, owner, repo, path, ref):
        for p, content in self._files_in_order:
            if p == path:
                return content
        return None

def _run(order):
    github = _FakeGitHub(order)
    nodes, edges = build_graph_data(github, "acme", "repo", "main")
    bridged = [n for n in nodes if n["external_key"] == "workflow::.github/workflows/_template-notify-slack.yml"]
    assert len(bridged) == 1, "collision must collapse to exactly one node"
    assert bridged[0]["node_type"] == "workflow"
    assert bridged[0]["display_name"] == "Notify Slack"
    assert bridged[0]["metadata"] == {"triggers": ["workflow_call"]}
    assert any(n["external_key"] == "job::.github/workflows/_template-notify-slack.yml::notify" for n in nodes)

def test_dedupe_when_callee_scanned_after_caller():
    _run([
        (".github/workflows/caller.yml", _CALLER),
        (".github/workflows/_template-notify-slack.yml", _CALLEE),
    ])

def test_dedupe_when_callee_scanned_before_caller():
    _run([
        (".github/workflows/_template-notify-slack.yml", _CALLEE),
        (".github/workflows/caller.yml", _CALLER),
    ])

def test_external_reusable_workflow_identity_is_shared_across_repos():
    external_key = "reusable_workflow::some-org/shared-workflows/.github/workflows/notify.yml@v2"
    identity_repo_a = _identity_key("acme", "reusable_workflow", external_key, "repo-a")
    identity_repo_b = _identity_key("acme", "reusable_workflow", external_key, "repo-b")
    assert identity_repo_a == identity_repo_b

def test_workflow_and_job_identity_still_repo_scoped():
    for node_type in ("workflow", "job", "composite_action"):
        key = f"{node_type}::.github/workflows/ci.yml"
        identity_repo_a = _identity_key("acme", node_type, key, "repo-a")
        identity_repo_b = _identity_key("acme", node_type, key, "repo-b")
        assert identity_repo_a != identity_repo_b
