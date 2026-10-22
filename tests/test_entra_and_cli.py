import json

import httpx
import respx

from entra_team_sync import cli
from entra_team_sync.entra import GRAPH, GraphSource
from fakes import BASE, FakeSemgrep, graph_user

TOKEN_URL = "https://login.microsoftonline.com/tenant/oauth2/v2.0/token"


@respx.mock
def test_graph_reads_transitive_members_and_owners_with_paging_and_retry(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    token = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "t"}))
    members_url = f"{GRAPH}/groups/g1/transitiveMembers/microsoft.graph.user"
    page2 = "https://graph.microsoft.com/v1.0/next-page-token"
    respx.get(members_url).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(200, json={"value": [graph_user("a", "alice@x.test")], "@odata.nextLink": page2}),
        ]
    )
    respx.get(page2).mock(return_value=httpx.Response(200, json={"value": [graph_user("b", None, "bob@x.test")]}))
    respx.get(f"{GRAPH}/groups/g1/owners/microsoft.graph.user").mock(
        return_value=httpx.Response(200, json={"value": [graph_user("c", "carol@x.test")]})
    )

    group = GraphSource("tenant", "client", "secret", client=httpx.Client()).group("g1")

    assert [u.id for u in group.members] == ["a", "b"]
    assert group.members[1].emails(("mail", "userPrincipalName")) == ["bob@x.test"]
    assert [u.id for u in group.owners] == ["c"]
    assert token.call_count == 1  # token reused across calls
    first = respx.calls[1].request
    assert first.headers["Authorization"] == "Bearer t"
    assert "%24select=" in str(first.url) and "accountEnabled" in str(first.url)


def _write(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return str(p)


def test_cli_plan_then_apply_with_snapshot(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SEMGREP_API_TOKEN", "tok")
    fake = FakeSemgrep(users={1: "alice@x.test", 2: "bob@x.test", 3: "old@x.test"})
    tid = fake.add_team("Platform", members={3: "TEAM_ROLE_MEMBER"})
    config = _write(tmp_path, "c.json", {"deployment_id": 1, "mappings": [{"entra_group_id": "g", "team": "Platform"}]})
    snap = _write(
        tmp_path,
        "s.json",
        {"groups": {"g": {"members": [graph_user("a", "alice@x.test")], "owners": [graph_user("b", "bob@x.test")]}}},
    )
    args = ["--config", config, "--entra-snapshot", snap, "--semgrep-url", BASE]

    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        assert cli.run(["plan", *args, "--json-out", str(tmp_path / "plan.json")]) == 0
        out = capsys.readouterr().out
        assert "+ add     alice@x.test as member" in out
        assert "+ add     bob@x.test as manager" in out
        assert "- remove  old@x.test (was member)" in out
        assert fake.patch_calls == []  # plan never writes
        assert json.loads((tmp_path / "plan.json").read_text())["total_removals"] == 1

        assert cli.run(["apply", *args]) == 0
        assert fake.members[tid] == {1: "TEAM_ROLE_MEMBER", 2: "TEAM_ROLE_MANAGER"}


def test_cli_apply_refuses_when_removal_guard_trips(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SEMGREP_API_TOKEN", "tok")
    fake = FakeSemgrep(users={i: f"u{i}@x.test" for i in range(1, 6)})
    fake.add_team("Platform", members={i: "TEAM_ROLE_MEMBER" for i in range(1, 6)})
    config = _write(
        tmp_path, "c.json", {"deployment_id": 1, "max_removals": 2, "mappings": [{"entra_group_id": "g", "team": "Platform"}]}
    )
    snap = _write(tmp_path, "s.json", {"groups": {"g": {"members": [graph_user("a", "u1@x.test")]}}})
    args = ["--config", config, "--entra-snapshot", snap, "--semgrep-url", BASE]
    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        assert cli.run(["apply", *args]) == 3
        assert fake.patch_calls == []
        assert cli.run(["apply", *args, "--allow-mass-removal"]) == 0
        assert len(fake.patch_calls) == 1


def test_cli_missing_token_is_usage_error(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("SEMGREP_API_TOKEN", raising=False)
    config = _write(tmp_path, "c.json", {"deployment_id": 1, "mappings": [{"entra_group_id": "g", "team": "t"}]})
    snap = _write(tmp_path, "s.json", {"groups": {}})
    assert cli.run(["plan", "--config", config, "--entra-snapshot", snap]) == 2
    assert "SEMGREP_API_TOKEN" in capsys.readouterr().err


def test_cli_snapshot_writes_groups(tmp_path):
    config = _write(tmp_path, "c.json", {"deployment_id": 1, "mappings": [{"entra_group_id": "g", "team": "t"}]})
    src = _write(tmp_path, "src.json", {"groups": {"g": {"members": [graph_user("a", "a@x.test")], "owners": []}}})
    out = tmp_path / "out.json"
    assert cli.run(["snapshot", "--config", config, "--entra-snapshot", src, "--out", str(out)]) == 0
    assert json.loads(out.read_text())["groups"]["g"]["members"][0]["mail"] == "a@x.test"
