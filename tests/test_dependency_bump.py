from app.analysis.dependency_bump import bump_manifest

_PACKAGE_JSON = """{
  "name": "widgets",
  "dependencies": {
    "lodash": "^4.17.15",
    "express": "^4.18.0"
  }
}"""

_REQUIREMENTS_TXT = "django==3.2.1\nrequests>=2.25.0\n"

def test_bumps_package_json_dependency():
    result = bump_manifest("package.json", _PACKAGE_JSON, "lodash", "4.17.21")
    assert result is not None
    assert '"lodash": "^4.17.21"' in result
    assert '"express": "^4.18.0"' in result

def test_bumps_requirements_txt_dependency():
    result = bump_manifest("requirements.txt", _REQUIREMENTS_TXT, "django", "3.2.25")
    assert result is not None
    assert "django==3.2.25" in result
    assert "requests>=2.25.0" in result

def test_returns_none_for_unknown_manifest():
    assert bump_manifest("go.mod", "module foo", "bar", "1.0.0") is None

def test_returns_none_when_package_not_present():
    assert bump_manifest("package.json", _PACKAGE_JSON, "not-a-dependency", "1.0.0") is None
