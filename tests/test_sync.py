import httpx
import pytest
import respx

from entra_team_sync.config import ConfigError, parse_config
from entra_team_sync.semgrep import SemgrepApi
from entra_team_sync.sync import apply_plan, build_plan
from fakes import BASE, FakeSemgrep, FakeSource, graph_user

ALICE, BOB, CAROL, DAVE, ERIN = 1, 2, 3, 4, 5
USERS = {
    ALICE: "alice@example.test",
    BOB: "bob@example.test",
    CAROL: "carol@example.test",
    DAVE: "dave@example.test",
    ERIN: "erin@example.test",
}


def cfg(mappings, **kw):
    return parse_config({"deployment_id": 1, "mappings": mappings, **kw})


@pytest.fixture
def fake():
    f = FakeSemgrep(users=dict(USERS))
    with respx.mock(assert_all_called=False) as router:
        f.mount(router)
        yield f


@pytest.fixture
def api(fake):
    return SemgrepApi("token", 1, BASE, client=httpx.Client())


def roles(fake, tid):
    return {u: r.removeprefix("TEAM_ROLE_").lower() for u, r in fake.members[tid].items()}


# --- plain group -> member, owners -> manager --------------------


def test_members_become_member_and_owners_become_manager(fake, api):
    tid = fake.add_team("Platform")
    source = FakeSource(
        {
            "g-platform": {
                "members": [graph_user("a", "alice@example.test"), graph_user("b", "bob@example.test")],
                "owners": [graph_user("c", "carol@example.test")],
            }
        }
    )
    plan = build_plan(cfg([{"entra_group_id": "g-platform", "team": "Platform"}]), api, source.group)
    result = apply_plan(plan, api)

    assert roles(fake, tid) == {ALICE: "member", BOB: "member", CAROL: "manager"}
    assert result.drift == [] and result.failed == []


def test_owner_who_is_also_member_gets_manager(fake, api):
    tid = fake.add_team("Platform")
    alice = graph_user("a", "alice@example.test")
    source = FakeSource({"g": {"members": [alice], "owners": [alice]}})
    apply_plan(build_plan(cfg([{"entra_group_id": "g", "team": "Platform"}]), api, source.group), api)
    assert roles(fake, tid) == {ALICE: "manager"}


def test_highest_role_wins_across_groups_for_same_team(fake, api):
    tid = fake.add_team("Platform")
    source = FakeSource(
        {
            "g-readers": {"members": [graph_user("a", "alice@example.test"), graph_user("b", "bob@example.test")]},
            "g-leads": {"members": [graph_user("b", "bob@example.test")]},
        }
    )
    config = cfg(
        [
            {"entra_group_id": "g-readers", "team": "Platform", "members_role": "readonly", "owners_role": None},
            {"entra_group_id": "g-leads", "team": tid, "members_role": "manager"},
        ]
    )
    plan = build_plan(config, api, source.group)
    assert len(plan.teams) == 1  # same team by name and by id is planned once
    apply_plan(plan, api)
    assert roles(fake, tid) == {ALICE: "readonly", BOB: "manager"}


def test_one_group_can_feed_several_teams(fake, api):
    t1, t2 = fake.add_team("Platform"), fake.add_team("Security")
    source = FakeSource({"g": {"members": [graph_user("a", "alice@example.test")]}})
    config = cfg([{"entra_group_id": "g", "team": "Platform"}, {"entra_group_id": "g", "team": "Security"}])
    apply_plan(build_plan(config, api, source.group), api)
    assert roles(fake, t1) == roles(fake, t2) == {ALICE: "member"}
    assert source.calls == ["g"]  # fetched once


# --- matching ------------------------------------------------------------------


def test_matches_on_upn_when_mail_differs_and_is_case_insensitive(fake, api):
    tid = fake.add_team("Platform")
    user = graph_user("a", mail="Alice.Smith@example.test", upn="ALICE@EXAMPLE.TEST")
    apply_plan(build_plan(cfg([{"entra_group_id": "g", "team": "Platform"}]), api, FakeSource({"g": {"members": [user]}}).group), api)
    assert roles(fake, tid) == {ALICE: "member"}


def test_unprovisioned_and_disabled_users_are_skipped_not_sent(fake, api):
    fake.add_team("Platform")
    source = FakeSource(
        {
            "g": {
                "members": [
                    graph_user("a", "alice@example.test"),
                    graph_user("z", "zoe@example.test"),  # not in Semgrep yet
                    graph_user("b", "bob@example.test", enabled=False),
                ]
            }
        }
    )
    plan = build_plan(cfg([{"entra_group_id": "g", "team": "Platform"}]), api, source.group)
    tp = plan.teams[0]
    assert tp.unmatched == ["zoe@example.test"]
    assert [c.user_id for c in tp.changes] == [ALICE]
    result = apply_plan(plan, api)
    assert result.failed == []  # an unknown id would have rejected the whole batch


def test_duplicate_semgrep_emails_are_skipped_with_warning(fake, api):
    fake.users[9] = "alice@example.test"
    fake.add_team("Platform")
    plan = build_plan(
        cfg([{"entra_group_id": "g", "team": "Platform"}]),
        api,
        FakeSource({"g": {"members": [graph_user("a", "alice@example.test")]}}).group,
    )
    assert plan.teams[0].changes == []
    assert "several Semgrep accounts" in plan.teams[0].warnings[0]


# --- diffs only, removals ------------------------------------------------------


def test_second_run_sends_nothing(fake, api):
    fake.add_team("Platform")
    source = FakeSource({"g": {"members": [graph_user("a", "alice@example.test")], "owners": [graph_user("c", "carol@example.test")]}})
    config = cfg([{"entra_group_id": "g", "team": "Platform"}])
    apply_plan(build_plan(config, api, source.group), api)
    calls = len(fake.patch_calls)

    plan = build_plan(config, api, source.group)
    assert all(not t.changes for t in plan.teams)
    apply_plan(plan, api)
    assert len(fake.patch_calls) == calls


def test_role_change_and_removal_use_current_role(fake, api):
    tid = fake.add_team(
        "Platform",
        members={ALICE: "TEAM_ROLE_MANAGER", BOB: "TEAM_ROLE_MEMBER", DAVE: "TEAM_ROLE_READONLY"},
    )
    source = FakeSource({"g": {"members": [graph_user("a", "alice@example.test"), graph_user("b", "bob@example.test")]}})
    result = apply_plan(build_plan(cfg([{"entra_group_id": "g", "team": "Platform"}]), api, source.group), api)

    assert roles(fake, tid) == {ALICE: "member", BOB: "member"}
    sent = {a["user_id"]: a for a in fake.patch_calls[-1]}
    assert sent[str(DAVE)] == {"user_id": str(DAVE), "action": "UPDATE_ACTION_REMOVE", "role": "TEAM_ROLE_READONLY"}
    assert sent[str(ALICE)]["action"] == "UPDATE_ACTION_UPDATE"
    assert str(BOB) not in sent  # unchanged users are never resent
    assert result.drift == []


def test_remove_unlisted_false_never_removes(fake, api):
    tid = fake.add_team("Platform", members={DAVE: "TEAM_ROLE_MEMBER"})
    source = FakeSource({"g": {"members": [graph_user("a", "alice@example.test")]}})
    config = cfg([{"entra_group_id": "g", "team": "Platform"}], remove_unlisted=False)
    apply_plan(build_plan(config, api, source.group), api)
    assert roles(fake, tid) == {ALICE: "member", DAVE: "member"}


def test_protected_emails_are_kept(fake, api):
    tid = fake.add_team("Platform", members={ERIN: "TEAM_ROLE_MANAGER"})
    source = FakeSource({"g": {"members": [graph_user("a", "alice@example.test")]}})
    config = cfg([{"entra_group_id": "g", "team": "Platform"}], protect_emails=["ERIN@example.test"])
    plan = build_plan(config, api, source.group)
    apply_plan(plan, api)
    assert plan.teams[0].protected == ["erin@example.test"]
    assert roles(fake, tid) == {ALICE: "member", ERIN: "manager"}


def test_empty_group_skips_removals_for_that_team(fake, api):
    tid = fake.add_team("Platform", members={ALICE: "TEAM_ROLE_MEMBER"})
    plan = build_plan(cfg([{"entra_group_id": "g", "team": "Platform"}]), api, FakeSource({"g": {"members": [], "owners": []}}).group)
    apply_plan(plan, api)
    assert "returned no users" in plan.teams[0].warnings[0]
    assert roles(fake, tid) == {ALICE: "member"}


def test_graph_failure_blocks_the_team_without_removals(fake, api):
    t1 = fake.add_team("Platform", members={ALICE: "TEAM_ROLE_MEMBER"})
    t2 = fake.add_team("Security")
    source = FakeSource(
        {"g-sec": {"members": [graph_user("b", "bob@example.test")]}},
        failing={"g-platform"},
    )
    config = cfg([{"entra_group_id": "g-platform", "team": "Platform"}, {"entra_group_id": "g-sec", "team": "Security"}])
    plan = build_plan(config, api, source.group)
    apply_plan(plan, api)
    assert "could not read Entra group" in plan.teams[0].error
    assert roles(fake, t1) == {ALICE: "member"}  # untouched
    assert roles(fake, t2) == {BOB: "member"}  # other teams still sync


def test_removal_guard_counts_all_teams(fake, api):
    fake.add_team("Platform", members={u: "TEAM_ROLE_MEMBER" for u in USERS})
    source = FakeSource({"g": {"members": [graph_user("a", "alice@example.test")]}})
    plan = build_plan(cfg([{"entra_group_id": "g", "team": "Platform"}], max_removals=3), api, source.group)
    assert plan.total_removals == 4 and plan.removal_guard_tripped


# --- team handling -------------------------------------------------------------


def test_scim_managed_and_default_teams_are_refused(fake, api):
    fake.add_team("Platform", scim_managed=True)
    fake.add_team("Everyone", default=True)
    source = FakeSource({"g": {"members": [graph_user("a", "alice@example.test")]}})
    config = cfg([{"entra_group_id": "g", "team": "Platform"}, {"entra_group_id": "g", "team": "Everyone"}])
    plan = build_plan(config, api, source.group)
    apply_plan(plan, api)
    assert "SCIM" in plan.teams[0].error and "default" in plan.teams[1].error
    assert fake.patch_calls == []


def test_missing_team_is_error_unless_create_team(fake, api):
    source = FakeSource({"g": {"members": [graph_user("a", "alice@example.test")]}})
    plan = build_plan(cfg([{"entra_group_id": "g", "team": "New Team"}]), api, source.group)
    assert "not found" in plan.teams[0].error

    plan = build_plan(cfg([{"entra_group_id": "g", "team": "New Team", "create_team": True}]), api, source.group)
    assert plan.teams[0].create
    result = apply_plan(plan, api)
    assert fake.created == ["New Team"] and result.drift == []
    tid = next(t for t, v in fake.teams.items() if v["name"] == "New Team")
    assert roles(fake, tid) == {ALICE: "member"}


def test_large_team_is_batched_and_paginated(fake, api):
    fake.users.update({1000 + i: f"user{i}@example.test" for i in range(1203)})
    fake.page_size_override = 400  # force several pages on every list call
    tid = fake.add_team("Big")
    members = [graph_user(f"u{i}", f"user{i}@example.test") for i in range(1203)]
    result = apply_plan(build_plan(cfg([{"entra_group_id": "g", "team": "Big"}]), api, FakeSource({"g": {"members": members}}).group), api)
    assert [len(c) for c in fake.patch_calls] == [500, 500, 203]
    assert len(fake.members[tid]) == 1203 and result.drift == []


def test_verify_reports_drift(fake, api):
    fake.add_team("Platform")
    source = FakeSource({"g": {"members": [graph_user("a", "alice@example.test")]}})
    plan = build_plan(cfg([{"entra_group_id": "g", "team": "Platform"}]), api, source.group)
    fake.drop_writes = True
    result = apply_plan(plan, api)
    assert result.drift and "expected-but-missing" in result.drift[0]


# --- users Semgrep refuses to add ---------------------------------------------


def _many(n, start=1000):
    return {start + i: f"user{i}@example.test" for i in range(n)}


def test_one_rejected_user_does_not_block_the_rest(fake, api):
    fake.users.update(_many(100))
    fake.not_members = {1042}
    tid = fake.add_team("Big")
    members = [graph_user(f"u{i}", f"user{i}@example.test") for i in range(100)]
    plan = build_plan(cfg([{"entra_group_id": "g", "team": "Big"}]), api, FakeSource({"g": {"members": members}}).group)

    result = apply_plan(plan, api)

    assert result.rejected == {1042: "user42@example.test"}
    assert len(fake.members[tid]) == 99 and 1042 not in fake.members[tid]
    assert result.drift == [] and result.failed == []
    # 1 rejected full batch + bisection, not 100 one-by-one calls
    assert len(fake.patch_calls) <= 2 * 7 + 1


def test_rejected_user_is_skipped_up_front_in_later_teams(fake, api):
    fake.not_members = {BOB}
    t1, t2 = fake.add_team("Platform"), fake.add_team("Security")
    source = FakeSource({"g": {"members": [graph_user("a", "alice@example.test"), graph_user("b", "bob@example.test")]}})
    config = cfg([{"entra_group_id": "g", "team": "Platform"}, {"entra_group_id": "g", "team": "Security"}])

    result = apply_plan(build_plan(config, api, source.group), api)

    assert result.rejected == {BOB: "bob@example.test"}
    assert roles(fake, t1) == roles(fake, t2) == {ALICE: "member"}
    # first team: rejected batch, then [alice] ok, [bob] rejected = 3 calls;
    # second team already knows bob is rejected: [alice] only = 1 call
    assert len(fake.patch_calls) == 4
    assert fake.patch_calls[-1] == [{"user_id": str(ALICE), "action": "UPDATE_ACTION_UPDATE", "role": "TEAM_ROLE_MEMBER"}]


def test_rejected_role_change_keeps_old_role_without_drift(fake, api):
    fake.not_members = {BOB}
    tid = fake.add_team("Platform", members={BOB: "TEAM_ROLE_MEMBER", DAVE: "TEAM_ROLE_MEMBER"})
    source = FakeSource({"g": {"owners": [graph_user("b", "bob@example.test")], "members": []}})
    # owners only -> bob should become manager; dave is removed
    config = cfg([{"entra_group_id": "g", "team": "Platform", "members_role": None}])

    result = apply_plan(build_plan(config, api, source.group), api)

    assert result.rejected == {BOB: "bob@example.test"}
    assert roles(fake, tid) == {BOB: "member"}  # removal of dave still applied
    assert result.drift == []


def test_other_400s_still_fail_the_team(fake, api):
    fake.add_team("Platform")
    fake.patch_error = "nope"
    source = FakeSource({"g": {"members": [graph_user("a", "alice@example.test")]}})
    result = apply_plan(build_plan(cfg([{"entra_group_id": "g", "team": "Platform"}]), api, source.group), api)
    assert result.failed and "nope" in result.failed[0] and result.rejected == {}


# --- config --------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, msg",
    [
        ({"mappings": [{"entra_group_id": "g", "team": "t"}]}, "deployment_id"),
        ({"deployment_id": 1, "mappings": []}, "non-empty"),
        ({"deployment_id": 1, "mappings": [{"team": "t"}]}, "entra_group_id"),
        ({"deployment_id": 1, "mappings": [{"entra_group_id": "g", "team": "t", "members_role": "owner"}]}, "role"),
        ({"deployment_id": 1, "mappings": [{"entra_group_id": "g", "team": "t", "members_role": None, "owners_role": None}]}, "both be null"),
        ({"deployment_id": 1, "mappings": [{"entra_group_id": "g", "team": True}]}, "team"),
    ],
)
def test_config_validation(raw, msg):
    with pytest.raises(ConfigError, match=msg):
        parse_config(raw)
