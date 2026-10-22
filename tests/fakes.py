"""In-memory Semgrep Teams API that mimics behaviour observed on the live API
(2026-10-22, test deployment):
- UPDATE adds a non-member or changes the role; repeating it is a no-op.
- REMOVE only removes when the given role matches the current one; otherwise 200 and nothing happens.
- Any unknown user id in an add/update rejects the whole request with 400.
- REMOVE of a non-member is a 200 no-op.
"""

from __future__ import annotations

import json

import httpx
import respx

BASE = "https://semgrep.test"


class FakeSemgrep:
    def __init__(self, deployment_id: int = 1, users: dict[int, str] | None = None):
        self.deployment_id = deployment_id
        self.users = users or {}  # id -> email
        self.teams: dict[int, dict] = {}
        self.members: dict[int, dict[int, str]] = {}
        self.patch_calls: list[list[dict]] = []
        self.created: list[str] = []
        self._next_team = 100
        self.page_size_override: int | None = None
        self.drop_writes = False  # simulate a write that returns 200 but changes nothing
        # Listed as deployment users but refused by the membership check (e.g. the
        # deployment's SSO provider changed since they were provisioned).
        self.not_members: set[int] = set()
        self.patch_error: str | None = None  # any other 400 on every PATCH

    def add_team(self, name: str, scim_managed: bool = False, default: bool = False, members=None) -> int:
        tid = self._next_team
        self._next_team += 1
        self.teams[tid] = {"id": str(tid), "name": name, "scimManaged": scim_managed, "isUserDefault": default}
        self.members[tid] = dict(members or {})
        return tid

    # --- handlers -------------------------------------------------------
    def _page(self, items, request, max_limit):
        limit = int(request.url.params.get("limit") or 100)
        if limit > max_limit:
            # Mirrors the live validator error (seen 2026-10-22 on the teams list).
            return None, httpx.Response(400, json={"error": "Invalid request", "violations": [f"limit <= {max_limit}"]})
        size = self.page_size_override or limit
        start = int(request.url.params.get("cursor") or 0)
        page = items[start : start + size]
        nxt = str(start + size) if start + size < len(items) else None
        return page, nxt

    def list_users(self, request):
        body = json.loads(request.content)
        size = self.page_size_override or body["options"]["pageSize"]
        start = int(body["options"].get("pageToken") or 0)
        items = [{"id": str(i), "email": e} for i, e in self.users.items()]
        page = items[start : start + size]
        resp = {"users": page}
        if start + size < len(items):
            resp["pageToken"] = str(start + size)
        return httpx.Response(200, json=resp)

    def list_teams(self, request):
        page, nxt = self._page(list(self.teams.values()), request, max_limit=100)
        if page is None:
            return nxt
        return httpx.Response(200, json={"teams": page, **({"cursor": nxt} if nxt else {})})

    def create_team(self, request):
        name = json.loads(request.content)["team"]["name"]
        tid = self.add_team(name)
        self.created.append(name)
        return httpx.Response(200, json={"team": self.teams[tid]})

    def list_members(self, request, team_id):
        items = [{"userId": str(u), "role": r} for u, r in self.members[int(team_id)].items()]
        page, nxt = self._page(items, request, max_limit=3000)
        if page is None:
            return nxt
        return httpx.Response(200, json={"users": page, **({"cursor": nxt} if nxt else {})})

    def patch_team(self, request, team_id):
        tid = int(team_id)
        actions = json.loads(request.content)["team"]["users"]
        if len(actions) > 500:
            return httpx.Response(400, json={"error": "Updating more than 500 users in a request is not allowed"})
        self.patch_calls.append(actions)
        if self.patch_error:
            return httpx.Response(400, json={"error": self.patch_error})
        for a in actions:
            uid = int(a["user_id"])
            if a["action"] == "UPDATE_ACTION_UPDATE" and (uid not in self.users or uid in self.not_members):
                return httpx.Response(400, json={"error": "User(s) must be members of the deployment"})
        if self.drop_writes:
            return httpx.Response(200, json={"team": self.teams[tid]})
        current = self.members[tid]
        for a in actions:
            uid = int(a["user_id"])
            if a["action"] == "UPDATE_ACTION_UPDATE":
                current[uid] = a["role"]
            elif a["action"] == "UPDATE_ACTION_REMOVE":
                if current.get(uid) == a["role"]:
                    del current[uid]
        return httpx.Response(200, json={"team": self.teams[tid]})

    def mount(self, router: respx.MockRouter) -> None:
        d = self.deployment_id
        router.post(f"{BASE}/api/agent/deployments/{d}/users/list").mock(side_effect=self.list_users)
        router.get(f"{BASE}/api/permissions/v2/deployments/{d}/teams").mock(side_effect=self.list_teams)
        router.post(f"{BASE}/api/permissions/v2/deployments/{d}/teams").mock(side_effect=self.create_team)
        router.get(url__regex=rf"{BASE}/api/permissions/v2/deployments/{d}/teams/(?P<team_id>\d+)/members").mock(
            side_effect=self.list_members
        )
        router.patch(url__regex=rf"{BASE}/api/permissions/v2/deployments/{d}/teams/(?P<team_id>\d+)$").mock(
            side_effect=self.patch_team
        )


def graph_user(uid: str, mail: str | None, upn: str | None = None, enabled: bool = True) -> dict:
    return {"id": uid, "mail": mail, "userPrincipalName": upn or mail, "accountEnabled": enabled}


class FakeSource:
    """Stand-in for GraphSource/SnapshotSource keyed by group id."""

    def __init__(self, groups: dict[str, dict], failing: set[str] | None = None):
        from entra_team_sync.entra import SnapshotSource

        self._snap = SnapshotSource.__new__(SnapshotSource)
        self._snap._groups = groups
        self._failing = failing or set()
        self.calls: list[str] = []

    def group(self, group_id: str):
        self.calls.append(group_id)
        if group_id in self._failing:
            raise RuntimeError("Graph 403 Authorization_RequestDenied")
        return self._snap.group(group_id)
