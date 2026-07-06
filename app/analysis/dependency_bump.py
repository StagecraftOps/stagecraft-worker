import re

def bump_package_json(content: str, package_name: str, fixed_version: str) -> str | None:
    pattern = re.compile(
        r'("' + re.escape(package_name) + r'"\s*:\s*")([^"]+)(")'
    )
    if not pattern.search(content):
        return None
    return pattern.sub(lambda m: f"{m.group(1)}^{fixed_version}{m.group(3)}", content, count=1)

def bump_requirements_txt(content: str, package_name: str, fixed_version: str) -> str | None:
    pattern = re.compile(
        r"(?im)^(" + re.escape(package_name) + r")\s*(==|>=|~=|>)\s*[^\s#]+"
    )
    if not pattern.search(content):
        return None
    return pattern.sub(lambda m: f"{m.group(1)}=={fixed_version}", content, count=1)

_BUMPERS = {
    "package.json": bump_package_json,
    "requirements.txt": bump_requirements_txt,
}

def bump_manifest(manifest_path: str, content: str, package_name: str, fixed_version: str) -> str | None:
    basename = manifest_path.rsplit("/", 1)[-1]
    bumper = _BUMPERS.get(basename)
    if not bumper:
        return None
    return bumper(content, package_name, fixed_version)
