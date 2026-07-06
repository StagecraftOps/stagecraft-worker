from app.analysis.template_diff import diff_workflow_against_template, narrate_diff

_TEMPLATE = """
name: Approved Template
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: ./.github/workflows/_template-security-scan.yml
      - uses: ./.github/workflows/_template-docker-build.yml
"""

def test_fully_compliant_workflow_scores_100():
    compliant = _TEMPLATE
    diff = diff_workflow_against_template(compliant, _TEMPLATE)
    assert diff["adoption_score"] == 100
    assert diff["missing_components"] == []

def test_missing_security_scan_detected():
    workflow = """
name: My Service
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: ./.github/workflows/_template-docker-build.yml
"""
    diff = diff_workflow_against_template(workflow, _TEMPLATE)
    assert "./.github/workflows/_template-security-scan.yml" in diff["missing_components"]

    assert diff["adoption_score"] == 67

def test_version_drift_detected():
    workflow = """
name: My Service
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: ./.github/workflows/_template-security-scan.yml
      - uses: ./.github/workflows/_template-docker-build.yml
"""
    diff = diff_workflow_against_template(workflow, _TEMPLATE)
    assert diff["missing_components"] == []
    assert diff["version_drift"] == [
        {"component": "actions/checkout", "template_version": "v4", "workflow_version": "v3"}
    ]

def test_extra_components_detected():
    workflow = _TEMPLATE + "      - uses: ./.github/actions/custom-notify\n"
    diff = diff_workflow_against_template(workflow, _TEMPLATE)
    assert "./.github/actions/custom-notify" in diff["extra_components"]

def test_narrate_diff_fully_compliant():
    diff = diff_workflow_against_template(_TEMPLATE, _TEMPLATE)
    narration = narrate_diff(diff, "ci.yml", "Approved Template")
    assert "fully adopts" in narration

def test_narrate_diff_with_gaps():
    workflow = "name: X\non: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps: []\n"
    diff = diff_workflow_against_template(workflow, _TEMPLATE)
    narration = narrate_diff(diff, "ci.yml", "Approved Template")
    assert "ci.yml" in narration
    assert "missing" in narration

def test_empty_template_yields_full_score():
    diff = diff_workflow_against_template(_TEMPLATE, "name: Empty\non: push\njobs: {}\n")
    assert diff["adoption_score"] == 100
