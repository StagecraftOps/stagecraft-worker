import httpx

from app.core.config import settings

_GH_API = "https://api.github.com"

class CopilotAgentClient:
    """Thin client for GitHub's Copilot coding agent (Agent Tasks REST API,
    public preview). Unlike GitHubRemediationClient, this authenticates with
    a flat fine-grained PAT bound to one human's Copilot seat -- the Agent
    Tasks API does not accept GitHub App installation tokens."""

    BASE_URL = _GH_API

    def __init__(self, pat: str | None = None) -> None:
        self._token = pat or settings.COPILOT_PAT
        self._client = httpx.Client(
            base_url=self.BASE_URL,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    def create_task(
        self, owner: str, repo: str, prompt: str, base_ref: str = "main", model: str | None = None,
    ) -> dict:
        payload: dict = {"prompt": prompt, "base_ref": base_ref, "create_pull_request": True}
        if model:
            payload["model"] = model
        response = self._client.post(f"/agents/repos/{owner}/{repo}/tasks", json=payload)
        response.raise_for_status()
        return response.json()

    def get_task(self, owner: str, repo: str, task_id: str) -> dict:
        response = self._client.get(f"/agents/repos/{owner}/{repo}/tasks/{task_id}")
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        self._client.close()
