import base64
import io
import zipfile

import httpx

class GitHubRemediationClient:

    BASE_URL = "https://api.github.com"

    def __init__(self, token: str) -> None:
        self._token = token
        self._client = httpx.Client(
            base_url=self.BASE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=60.0,
            follow_redirects=True,
        )

    def _get(self, path: str, **kwargs) -> dict | list:
        response = self._client.get(path, **kwargs)
        response.raise_for_status()
        return response.json()

    def _post(self, path: str, **kwargs) -> dict:
        response = self._client.post(path, **kwargs)
        response.raise_for_status()
        return response.json()

    def _put(self, path: str, **kwargs) -> dict:
        response = self._client.put(path, **kwargs)
        response.raise_for_status()
        return response.json()

    def get_org_repos(self, org: str, per_page: int = 100) -> list[dict]:
        repos: list[dict] = []
        page = 1
        while True:
            batch = self._get(
                f"/orgs/{org}/repos",
                params={"per_page": per_page, "page": page, "type": "all"},
            )
            if not batch:
                break
            repos.extend(batch)
            if len(batch) < per_page:
                break
            page += 1
        return repos

    def get_repo_runs(
        self, owner: str, repo: str, per_page: int = 100, page: int = 1
    ) -> tuple[list[dict], int | None]:
        response = self._client.get(
            f"/repos/{owner}/{repo}/actions/runs",
            params={"per_page": per_page, "page": page},
        )
        response.raise_for_status()
        data = response.json()
        remaining = response.headers.get("X-RateLimit-Remaining")
        return data.get("workflow_runs", []), int(remaining) if remaining is not None else None

    def get_run_jobs(self, owner: str, repo: str, run_id: int) -> list[dict]:
        response = self._client.get(f"/repos/{owner}/{repo}/actions/runs/{run_id}/jobs")
        response.raise_for_status()
        return response.json().get("jobs", [])

    def get_run_logs(self, owner: str, repo: str, run_id: int) -> str:
        response = self._client.get(
            f"/repos/{owner}/{repo}/actions/runs/{run_id}/logs",
            follow_redirects=True,
        )
        response.raise_for_status()

        zip_bytes = io.BytesIO(response.content)
        all_lines: list[str] = []

        try:
            with zipfile.ZipFile(zip_bytes) as zf:
                for name in sorted(zf.namelist()):
                    if name.endswith(".txt"):
                        with zf.open(name) as f:
                            content = f.read().decode("utf-8", errors="replace")
                            all_lines.extend(content.splitlines())
        except zipfile.BadZipFile:
            all_lines = response.text.splitlines()

        last_300 = all_lines[-300:] if len(all_lines) > 300 else all_lines
        return "\n".join(last_300)

    def get_pull_request_author(self, owner: str, repo: str, pr_number: int) -> str | None:
        try:
            data = self._get(f"/repos/{owner}/{repo}/pulls/{pr_number}")
            if isinstance(data, dict):
                user = data.get("user") or {}
                return user.get("login")
        except Exception:
            return None
        return None

    def get_pull_request_diff(self, owner: str, repo: str, pr_number: int) -> str:
        response = self._client.get(
            f"/repos/{owner}/{repo}/pulls/{pr_number}",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github.diff",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        response.raise_for_status()
        return response.text

    def get_repo_tree(self, owner: str, repo: str, ref: str) -> list[dict]:
        data = self._get(f"/repos/{owner}/{repo}/git/trees/{ref}", params={"recursive": "1"})
        return data.get("tree", []) if isinstance(data, dict) else []

    def get_file_content(self, owner: str, repo: str, path: str, ref: str) -> str | None:
        try:
            response = self._client.get(
                f"/repos/{owner}/{repo}/contents/{path}",
                params={"ref": ref},
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/vnd.github.raw+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.text
        except httpx.HTTPStatusError:
            return None

    def get_workflow_yaml(self, owner: str, repo: str, path: str, ref: str) -> str:
        response = self._client.get(
            f"/repos/{owner}/{repo}/contents/{path}",
            params={"ref": ref},
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github.raw+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        response.raise_for_status()
        return response.text

    def get_file_sha(self, owner: str, repo: str, path: str, ref: str) -> str | None:
        try:
            data = self._get(f"/repos/{owner}/{repo}/contents/{path}", params={"ref": ref})
            if isinstance(data, dict):
                return data.get("sha")
            return None
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise

    def create_fix_branch(
        self, owner: str, repo: str, base_sha: str, branch_name: str
    ) -> None:
        self._post(
            f"/repos/{owner}/{repo}/git/refs",
            json={"ref": f"refs/heads/{branch_name}", "sha": base_sha},
        )

    def commit_fix(
        self,
        owner: str,
        repo: str,
        branch: str,
        path: str,
        content: str,
        message: str,
        current_sha: str | None,
    ) -> None:
        encoded_content = base64.b64encode(content.encode("utf-8")).decode("utf-8")
        payload: dict = {
            "message": message,
            "content": encoded_content,
            "branch": branch,
        }
        if current_sha:
            payload["sha"] = current_sha

        self._put(f"/repos/{owner}/{repo}/contents/{path}", json=payload)

    def create_pr(
        self,
        owner: str,
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> dict:
        return self._post(
            f"/repos/{owner}/{repo}/pulls",
            json={
                "title": title,
                "body": body,
                "head": head,
                "base": base,
                "maintainer_can_modify": True,
            },
        )

    def get_default_branch(self, owner: str, repo: str) -> str:
        data = self._get(f"/repos/{owner}/{repo}")
        return data.get("default_branch", "main") if isinstance(data, dict) else "main"

    def get_branch_sha(self, owner: str, repo: str, branch: str) -> str | None:
        try:
            data = self._get(f"/repos/{owner}/{repo}/git/ref/heads/{branch}")
            if isinstance(data, dict):
                return data.get("object", {}).get("sha")
            return None
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise

    def search_code(self, query: str) -> list[dict]:
        results = self._get("/search/code", params={"q": query})
        return results.get("items", []) if isinstance(results, dict) else []

    def find_issue_by_marker(self, owner: str, repo: str, marker: str) -> dict | None:
        # The /search/issues endpoint returns 403 for GitHub App installation
        # tokens (search has separate, stricter auth requirements than the
        # regular REST API). List issues instead and scan bodies client-side --
        # capped at a few pages since this is a dedup check, not a full audit.
        for page in range(1, 4):
            issues = self._get(
                f"/repos/{owner}/{repo}/issues",
                params={"state": "all", "per_page": 100, "page": page, "labels": "security"},
            )
            if not isinstance(issues, list) or not issues:
                break
            for issue in issues:
                if marker in (issue.get("body") or ""):
                    return issue
            if len(issues) < 100:
                break
        return None

    def create_issue(
        self, owner: str, repo: str, title: str, body: str, labels: list[str] | None = None
    ) -> dict:
        payload: dict = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        return self._post(f"/repos/{owner}/{repo}/issues", json=payload)

    def add_issue_comment(self, owner: str, repo: str, issue_number: int, body: str) -> dict:
        return self._post(f"/repos/{owner}/{repo}/issues/{issue_number}/comments", json={"body": body})

    def add_pr_labels(self, owner: str, repo: str, pr_number: int, labels: list[str]) -> None:
        self._post(f"/repos/{owner}/{repo}/issues/{pr_number}/labels", json={"labels": labels})

    def get_code_scanning_alert(self, owner: str, repo: str, alert_number: int) -> dict:
        return self._get(f"/repos/{owner}/{repo}/code-scanning/alerts/{alert_number}")

    def dispatch_workflow(
        self, owner: str, repo: str, workflow_file: str, ref: str, inputs: dict | None = None
    ) -> None:
        response = self._client.post(
            f"/repos/{owner}/{repo}/actions/workflows/{workflow_file}/dispatches",
            json={"ref": ref, "inputs": inputs or {}},
        )
        response.raise_for_status()

    def close(self) -> None:
        self._client.close()
