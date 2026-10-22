"""Work out each team's desired membership and the minimal changes to reach it.

Only real changes are sent. The Teams API counts requested actions rather than
applied ones when it updates a team's member count, so resending unchanged
users on every run would make that count drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from entra_team_sync.config import API_ROLE, ROLE_FROM_API, ROLE_PRIORITY, Config, Mapping
from entra_team_sync.entra import GroupSnapshot
from entra_team_sync.semgrep import SemgrepApi, Team, UserAction

UPDATE = "UPDATE_ACTION_UPDATE"
REMOVE = "UPDATE_ACTION_REMOVE"


@dataclass(frozen=True)
class Change:
    user_id: int
    email: str
    old_role: str | None  # manager/member/readonly, None if not on the team
    new_role: str | None  # None means remove

    @property
    def action(self) -> UserAction:
        if self.new_role is None:
            # REMOVE must name the user's current role; a mismatched role is a silent no-op.
            return UserAction(self.user_id, REMOVE, API_ROLE[self.old_role])
        return UserAction(self.user_id, UPDATE, API_ROLE[self.new_role])


@dataclass
class TeamPlan:
    label: str  # team name or id as written in the config
    team: Team | None = None
    create: bool = False
    changes: list[Change] = field(default_factory=list)
    # Entra users with no matching Semgrep account (not provisioned yet, or email mismatch).
    unmatched: list[str] = field(default_factory=list)
    protected: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    # Expected (user id -> role) after apply, used to verify.
    expected: dict[int, str] = field(default_factory=dict)

    @property
    def removals(self) -> int:
        return sum(1 for c in self.changes if c.new_role is None)


@dataclass
class Plan:
    teams: list[TeamPlan]
    max_removals: int

    @property
    def total_removals(self) -> int:
        return sum(t.removals for t in self.teams if t.error is None)

    @property
    def removal_guard_tripped(self) -> bool:
        return self.total_removals > self.max_removals


def _team_key(team: str | int) -> str:
    return f"id:{team}" if isinstance(team, int) else f"name:{team.strip().lower()}"


def build_plan(config: Config, api: SemgrepApi, fetch_group) -> Plan:
    """fetch_group(group_id) -> GroupSnapshot; may raise."""
    users = api.list_users()
    by_email: dict[str, int] = {}
    ambiguous: set[str] = set()
    for u in users:
        if not u.email:
            continue
        if u.email in by_email and by_email[u.email] != u.id:
            ambiguous.add(u.email)
        by_email[u.email] = u.id
    email_by_id = {u.id: u.email for u in users}

    teams = api.list_teams()
    teams_by_id = {t.id: t for t in teams}
    teams_by_name = {t.name.strip().lower(): t for t in teams}

    def resolve(team_ref: str | int) -> Team | None:
        if isinstance(team_ref, int):
            return teams_by_id.get(team_ref)
        return teams_by_name.get(team_ref.strip().lower())

    # Group by the resolved team, so a team named in one mapping and referenced by
    # id in another is planned once.
    grouped: dict[str, list[Mapping]] = {}
    for m in config.mappings:
        resolved = resolve(m.team)
        key = f"id:{resolved.id}" if resolved else _team_key(m.team)
        grouped.setdefault(key, []).append(m)

    group_cache: dict[str, GroupSnapshot | Exception] = {}

    def get_group(group_id: str) -> GroupSnapshot:
        if group_id not in group_cache:
            try:
                group_cache[group_id] = fetch_group(group_id)
            except Exception as e:  # noqa: BLE001 - reported per team, never turns into removals
                group_cache[group_id] = e
        result = group_cache[group_id]
        if isinstance(result, Exception):
            raise result
        return result

    plans = []
    for mappings in grouped.values():
        label = str(mappings[0].team)
        plan = TeamPlan(label=label)
        plans.append(plan)

        team_ref = mappings[0].team
        team = resolve(team_ref)
        if team is None:
            if isinstance(team_ref, str) and any(m.create_team for m in mappings):
                plan.create = True
            else:
                plan.error = "team not found in Semgrep (set create_team: true to create it by name)"
                continue
        elif team.scim_managed:
            plan.error = "team is managed by the SCIM directory ([team] suffix group); the API cannot edit it"
            continue
        elif team.is_default:
            plan.error = "default teams are not supported"
            continue
        plan.team = team

        desired: dict[int, str] = {}
        desired_email: dict[int, str] = {}
        empty_groups = []
        try:
            for m in mappings:
                group = get_group(m.entra_group_id)
                if not group.members and not group.owners:
                    empty_groups.append(m.entra_group_id)
                sources = [(group.members, m.members_role), (group.owners, m.owners_role)]
                for entra_users, role in sources:
                    if role is None:
                        continue
                    for eu in entra_users:
                        if not eu.enabled:
                            continue
                        emails = eu.emails(config.match_attributes)
                        hit = next((e for e in emails if e in by_email and e not in ambiguous), None)
                        if hit is None:
                            label_email = emails[0] if emails else eu.id
                            if any(e in ambiguous for e in emails):
                                plan.warnings.append(f"{label_email}: several Semgrep accounts share this email; skipped")
                            elif label_email not in plan.unmatched:
                                plan.unmatched.append(label_email)
                            continue
                        uid = by_email[hit]
                        if uid not in desired or ROLE_PRIORITY[role] > ROLE_PRIORITY[desired[uid]]:
                            desired[uid] = role
                            desired_email[uid] = hit
        except Exception as e:  # noqa: BLE001
            plan.error = f"could not read Entra group: {e}"
            continue

        current: dict[int, str] = {}
        if team is not None:
            current = {uid: ROLE_FROM_API.get(r, r) for uid, r in api.list_members(team.id).items()}

        for uid, role in sorted(desired.items()):
            if current.get(uid) != role:
                plan.changes.append(Change(uid, desired_email[uid], current.get(uid), role))

        expected = dict(current) | desired
        if config.remove_unlisted:
            if empty_groups:
                # An empty group is far more often a permissions or sync problem than a
                # real "remove everyone", so this team gets no removals this run.
                plan.warnings.append(
                    f"group(s) {', '.join(empty_groups)} returned no users; removals skipped for this team"
                )
            else:
                for uid, role in sorted(current.items()):
                    if uid in desired:
                        continue
                    email = email_by_id.get(uid, "")
                    if email in config.protect_emails:
                        plan.protected.append(email)
                        continue
                    if role not in API_ROLE:
                        plan.warnings.append(f"user {uid} has unknown role {role}; left alone")
                        continue
                    plan.changes.append(Change(uid, email, role, None))
                    expected.pop(uid)
        plan.expected = expected

    return Plan(plans, config.max_removals)


@dataclass
class ApplyResult:
    applied: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    drift: list[str] = field(default_factory=list)


def apply_plan(plan: Plan, api: SemgrepApi) -> ApplyResult:
    result = ApplyResult()
    for tp in plan.teams:
        if tp.error or (not tp.changes and not tp.create):
            continue
        try:
            if tp.create:
                tp.team = api.create_team(tp.label)
            if tp.changes:
                # Removals and role changes first, then additions: keeps a user from
                # briefly holding two roles if the API ever processes them in order.
                ordered = sorted(tp.changes, key=lambda c: c.old_role is None)
                api.apply_user_actions(tp.team.id, [c.action for c in ordered])
            result.applied.append(tp.label)
        except Exception as e:  # noqa: BLE001 - keep going with other teams
            result.failed.append(f"{tp.label}: {e}")
            continue

        after = {uid: ROLE_FROM_API.get(r, r) for uid, r in api.list_members(tp.team.id).items()}
        if after != tp.expected:
            missing = {u: r for u, r in tp.expected.items() if after.get(u) != r}
            extra = {u: r for u, r in after.items() if u not in tp.expected}
            result.drift.append(f"{tp.label}: expected-but-missing={missing} unexpected={extra}")
    return result
