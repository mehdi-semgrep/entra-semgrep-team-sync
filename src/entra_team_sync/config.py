"""Mapping file: which Entra group feeds which Semgrep team, with which roles."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# Semgrep team roles, highest privilege first. When a user reaches the same team
# through several groups (or as owner and member), the highest role wins.
ROLE_PRIORITY = {"manager": 3, "member": 2, "readonly": 1}
API_ROLE = {
    "manager": "TEAM_ROLE_MANAGER",
    "member": "TEAM_ROLE_MEMBER",
    "readonly": "TEAM_ROLE_READONLY",
}
ROLE_FROM_API = {v: k for k, v in API_ROLE.items()}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Mapping:
    entra_group_id: str
    team: str | int  # Semgrep team name, or numeric team id
    members_role: str | None = "member"
    owners_role: str | None = "manager"
    create_team: bool = False


@dataclass(frozen=True)
class Config:
    deployment_id: int
    mappings: list[Mapping]
    # Remove team members who are not in any mapped group for that team.
    remove_unlisted: bool = True
    # Abort the whole run if it would remove more than this many memberships.
    max_removals: int = 25
    # Never remove these users (case-insensitive emails), e.g. break-glass admins.
    protect_emails: frozenset[str] = field(default_factory=frozenset)
    # Entra user attributes compared against the Semgrep account email.
    match_attributes: tuple[str, ...] = ("mail", "userPrincipalName")


def _role(value: object, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value.lower() not in ROLE_PRIORITY:
        raise ConfigError(f"{where}: role must be one of {sorted(ROLE_PRIORITY)} or null, got {value!r}")
    return value.lower()


def parse_config(raw: dict) -> Config:
    if not isinstance(raw.get("deployment_id"), int):
        raise ConfigError("deployment_id must be an integer")
    raw_mappings = raw.get("mappings")
    if not isinstance(raw_mappings, list) or not raw_mappings:
        raise ConfigError("mappings must be a non-empty list")

    mappings = []
    for i, m in enumerate(raw_mappings):
        where = f"mappings[{i}]"
        group_id = m.get("entra_group_id")
        team = m.get("team")
        if not isinstance(group_id, str) or not group_id:
            raise ConfigError(f"{where}: entra_group_id must be a non-empty string")
        if isinstance(team, bool) or not isinstance(team, str | int) or team == "":
            raise ConfigError(f"{where}: team must be a team name or numeric team id")
        members_role = _role(m.get("members_role", "member"), f"{where}.members_role")
        owners_role = _role(m.get("owners_role", "manager"), f"{where}.owners_role")
        if members_role is None and owners_role is None:
            raise ConfigError(f"{where}: members_role and owners_role cannot both be null")
        mappings.append(
            Mapping(
                entra_group_id=group_id,
                team=team,
                members_role=members_role,
                owners_role=owners_role,
                create_team=bool(m.get("create_team", False)),
            )
        )

    max_removals = raw.get("max_removals", 25)
    if not isinstance(max_removals, int) or max_removals < 0:
        raise ConfigError("max_removals must be a non-negative integer")
    match_attributes = tuple(raw.get("match_attributes", ("mail", "userPrincipalName")))
    if not match_attributes:
        raise ConfigError("match_attributes cannot be empty")

    return Config(
        deployment_id=raw["deployment_id"],
        mappings=mappings,
        remove_unlisted=bool(raw.get("remove_unlisted", True)),
        max_removals=max_removals,
        protect_emails=frozenset(e.lower() for e in raw.get("protect_emails", [])),
        match_attributes=match_attributes,
    )


def load_config(path: Path) -> Config:
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    return parse_config(raw)
