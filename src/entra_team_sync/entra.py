"""Read group members and owners from Microsoft Graph (app-only auth).

Required application permissions, with admin consent:
  GroupMember.Read.All, User.Read.All
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import httpx

from entra_team_sync import http

GRAPH = "https://graph.microsoft.com/v1.0"
USER_FIELDS = "id,displayName,mail,userPrincipalName,accountEnabled"


@dataclass(frozen=True)
class EntraUser:
    id: str
    attributes: dict[str, str | None]  # mail, userPrincipalName, ...
    enabled: bool = True

    def emails(self, match_attributes: tuple[str, ...]) -> list[str]:
        out = []
        for attr in match_attributes:
            value = self.attributes.get(attr)
            if value and value.lower() not in out:
                out.append(value.lower())
        return out


@dataclass(frozen=True)
class GroupSnapshot:
    group_id: str
    members: list[EntraUser]
    owners: list[EntraUser]


def _to_user(raw: dict) -> EntraUser:
    return EntraUser(
        id=raw["id"],
        attributes={k: raw.get(k) for k in ("mail", "userPrincipalName")},
        enabled=raw.get("accountEnabled", True) is not False,
    )


class GraphSource:
    def __init__(
        self,
        tenant_id: str = "",
        client_id: str = "",
        client_secret: str = "",
        client: httpx.Client | None = None,
        *,
        access_token: str | None = None,
        page_size: int = 999,
    ):
        """App-only auth from tenant/client/secret, or a ready Graph `access_token`
        (testing only, e.g. from `az account get-access-token --resource-type ms-graph`)."""
        self._client = client or httpx.Client(timeout=30)
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str | None = access_token
        self._page_size = page_size

    def _auth_header(self) -> dict[str, str]:
        if self._token is None:
            resp = http.request(
                self._client,
                "POST",
                f"https://login.microsoftonline.com/{self._tenant_id}/oauth2/v2.0/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "scope": "https://graph.microsoft.com/.default",
                },
            )
            self._token = resp.json()["access_token"]
        return {"Authorization": f"Bearer {self._token}"}

    def _list_users(self, url: str) -> list[EntraUser]:
        users = []
        next_url: str | None = url
        params: dict | None = {"$select": USER_FIELDS, "$top": str(self._page_size)}
        while next_url:
            resp = http.request(self._client, "GET", next_url, params=params, headers=self._auth_header())
            body = resp.json()
            users.extend(_to_user(u) for u in body.get("value", []))
            next_url = body.get("@odata.nextLink")
            params = None  # nextLink already carries the query
        return users

    def group(self, group_id: str) -> GroupSnapshot:
        # transitiveMembers so nested groups count; the cast keeps users only.
        members = self._list_users(f"{GRAPH}/groups/{group_id}/transitiveMembers/microsoft.graph.user")
        owners = self._list_users(f"{GRAPH}/groups/{group_id}/owners/microsoft.graph.user")
        return GroupSnapshot(group_id, members, owners)


class SnapshotSource:
    """Offline source: a JSON file shaped like the `snapshot` command's output.

    {"groups": {"<group id>": {"members": [<graph user>], "owners": [<graph user>]}}}
    """

    def __init__(self, path: Path):
        self._groups = json.loads(path.read_text())["groups"]

    def group(self, group_id: str) -> GroupSnapshot:
        if group_id not in self._groups:
            raise KeyError(f"group {group_id} not in snapshot")
        g = self._groups[group_id]
        return GroupSnapshot(
            group_id,
            [_to_user(u) for u in g.get("members", [])],
            [_to_user(u) for u in g.get("owners", [])],
        )


def snapshot_to_json(groups: list[GroupSnapshot]) -> dict:
    def dump(u: EntraUser) -> dict:
        return {"id": u.id, **u.attributes, "accountEnabled": u.enabled}

    return {
        "groups": {
            g.group_id: {"members": [dump(u) for u in g.members], "owners": [dump(u) for u in g.owners]}
            for g in groups
        }
    }
