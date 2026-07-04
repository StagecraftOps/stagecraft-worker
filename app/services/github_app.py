import time

import httpx
import jwt as pyjwt

from app.core.config import settings

_GH_API = "https://api.github.com"


def _make_app_jwt() -> str:
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 600, "iss": settings.GITHUB_APP_ID}
    pem = settings.GITHUB_APP_PRIVATE_KEY.replace("\\n", "\n")
    return pyjwt.encode(payload, pem, algorithm="RS256")


async def get_installation_id_for_org(org_login: str) -> int | None:
    """Resolve an org's installation ID straight from GitHub via the App JWT.

    Lets the worker mint a token for any org the App is installed on without a
    pre-seeded organizations row (that row is normally created by the
    installation webhook, which itself needs a logged-in user to own it).
    Returns None if the App isn't installed on the org.
    """
    app_jwt = _make_app_jwt()
    headers = {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{_GH_API}/orgs/{org_login}/installation", headers=headers)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()["id"]


async def get_installation_token(installation_id: int) -> str:
    """Exchange an installation ID for a short-lived installation access token."""
    app_jwt = _make_app_jwt()
    headers = {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{_GH_API}/app/installations/{installation_id}/access_tokens",
            headers=headers,
            json={"permissions": {"contents": "write", "pull_requests": "write", "actions": "read"}},
        )
        r.raise_for_status()
        return r.json()["token"]


def github_app_configured() -> bool:
    return bool(settings.GITHUB_APP_ID and settings.GITHUB_APP_PRIVATE_KEY)
