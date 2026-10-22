"""Semgrep public API calls used by the sync (Web API token required)."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from entra_team_sync import http
from entra_team_sync.http import ApiError

# API limits (teams.proto validators): team PATCH takes at most 500 user actions,
# team listing at most 100 per page, member listing at most 3000 per page.
MAX_USER_ACTIONS_PER_REQUEST = 500
TEAMS_PAGE_SIZE = 100
MEMBERS_PAGE_SIZE = 1000
# The user listing has no documented cap; stay at the server's default page size.
USERS_PAGE_SIZE = 100


class NotDeploymentMember(Exception):
    """The PATCH was rejected because a user being added is not a deployment member.

    Semgrep rejects the whole request in that case and applies nothing.
    """


@dataclass(frozen=True)
class SemgrepUser:
    id: int
    email: str


@dataclass(frozen=True)
class Team:
    id: int
    name: str
    scim_managed: bool
    is_default: bool


@dataclass(frozen=True)
class UserAction:
    user_id: int
    action: str  # UPDATE_ACTION_UPDATE | UPDATE_ACTION_REMOVE
    role: str  # TEAM_ROLE_*

    def to_json(self) -> dict:
        return {"user_id": str(self.user_id), "action": self.action, "role": self.role}


def _team(raw: dict) -> Team:
    return Team(
        id=int(raw["id"]),
        name=raw["name"],
        scim_managed=bool(raw.get("scimManaged", False)),
        is_default=bool(raw.get("isUserDefault") or raw.get("isProjectDefault")),
    )


class SemgrepApi:
    def __init__(self, token: str, deployment_id: int, base_url: str = "https://semgrep.dev", client: httpx.Client | None = None):
        self._client = client or httpx.Client(timeout=60)
        self._headers = {"Authorization": f"Bearer {token}"}
        self._base = base_url.rstrip("/")
        self.deployment_id = deployment_id

    def _call(self, method: str, path: str, **kwargs) -> dict:
        resp = http.request(self._client, method, f"{self._base}{path}", headers=self._headers, **kwargs)
        return resp.json() if resp.content else {}

    def list_users(self) -> list[SemgrepUser]:
        users, token = [], ""
        while True:
            body = self._call(
                "POST",
                f"/api/agent/deployments/{self.deployment_id}/users/list",
                json={"options": {"pageSize": USERS_PAGE_SIZE, "pageToken": token}},
            )
            users.extend(SemgrepUser(int(u["id"]), (u.get("email") or "").lower()) for u in body.get("users", []))
            token = body.get("pageToken") or ""
            if not token:
                return users

    def list_teams(self) -> list[Team]:
        teams, cursor = [], None
        while True:
            params = {"limit": TEAMS_PAGE_SIZE, **({"cursor": cursor} if cursor else {})}
            body = self._call("GET", f"/api/permissions/v2/deployments/{self.deployment_id}/teams", params=params)
            teams.extend(_team(t) for t in body.get("teams", []))
            cursor = body.get("cursor")
            if not cursor:
                return teams

    def create_team(self, name: str) -> Team:
        body = self._call("POST", f"/api/permissions/v2/deployments/{self.deployment_id}/teams", json={"team": {"name": name}})
        return _team(body["team"])

    def list_members(self, team_id: int) -> dict[int, str]:
        """user id -> TEAM_ROLE_*"""
        members, cursor = {}, None
        while True:
            params = {"limit": MEMBERS_PAGE_SIZE, **({"cursor": cursor} if cursor else {})}
            body = self._call(
                "GET", f"/api/permissions/v2/deployments/{self.deployment_id}/teams/{team_id}/members", params=params
            )
            for u in body.get("users", []):
                members[int(u["userId"])] = u["role"]
            cursor = body.get("cursor")
            if not cursor:
                return members

    def apply_user_actions(self, team_id: int, actions: list[UserAction]) -> None:
        """One PATCH; callers keep batches at or below MAX_USER_ACTIONS_PER_REQUEST."""
        try:
            self._call(
                "PATCH",
                f"/api/permissions/v2/deployments/{self.deployment_id}/teams/{team_id}",
                json={"team": {"users": [a.to_json() for a in actions]}},
            )
        except ApiError as e:
            if e.status == 400 and "must be members of the deployment" in e.body:
                raise NotDeploymentMember(e.body) from e
            raise
