"""entra-team-sync plan|apply|snapshot

Credentials come from environment variables only:
  SEMGREP_API_TOKEN                                   (Semgrep token with Web API access)
  AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET (Entra app registration)
  AZURE_GRAPH_ACCESS_TOKEN   (testing only: use a ready Graph token instead of the app)

Exit codes: 0 ok, 1 team errors / failed, rejected or unverified writes, 2 usage or config
error, 3 removal guard tripped (nothing written).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from entra_team_sync.config import ConfigError, load_config
from entra_team_sync.entra import GraphSource, SnapshotSource, snapshot_to_json
from entra_team_sync.semgrep import SemgrepApi
from entra_team_sync.sync import Plan, apply_plan, build_plan


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"environment variable {name} is not set")
    return value


def _entra_source(args):
    if args.entra_snapshot:
        return SnapshotSource(Path(args.entra_snapshot))
    page_size = args.graph_page_size
    # Testing only: a ready Graph token (e.g. from the az CLI) instead of app credentials.
    token = os.environ.get("AZURE_GRAPH_ACCESS_TOKEN", "").strip()
    if token:
        return GraphSource(access_token=token, page_size=page_size)
    return GraphSource(_env("AZURE_TENANT_ID"), _env("AZURE_CLIENT_ID"), _env("AZURE_CLIENT_SECRET"), page_size=page_size)


def _print_plan(plan: Plan) -> None:
    for tp in plan.teams:
        team_desc = f"team {tp.team.name!r} (id {tp.team.id})" if tp.team else f"team {tp.label!r}"
        if tp.create:
            team_desc += " [will be created]"
        print(f"\n== {team_desc}")
        if tp.error:
            print(f"   ERROR: {tp.error}")
            continue
        if not tp.changes:
            print("   no changes")
        for c in tp.changes:
            if c.new_role is None:
                print(f"   - remove  {c.email or c.user_id} (was {c.old_role})")
            elif c.old_role is None:
                print(f"   + add     {c.email} as {c.new_role}")
            else:
                print(f"   ~ change  {c.email}: {c.old_role} -> {c.new_role}")
        for w in tp.warnings:
            print(f"   warning: {w}")
        if tp.unmatched:
            print(f"   not in Semgrep yet (skipped): {', '.join(sorted(tp.unmatched))}")
        if tp.protected:
            print(f"   protected, not removed: {', '.join(sorted(tp.protected))}")
    print(
        f"\nSummary: {sum(len(t.changes) for t in plan.teams if not t.error)} change(s), "
        f"{plan.total_removals} removal(s) (limit {plan.max_removals}), "
        f"{sum(1 for t in plan.teams if t.error)} team error(s)"
    )


def _plan_json(plan: Plan) -> dict:
    return {
        "teams": [
            {
                "team": tp.label,
                "team_id": tp.team.id if tp.team else None,
                "create": tp.create,
                "error": tp.error,
                "changes": [asdict(c) for c in tp.changes],
                "unmatched": tp.unmatched,
                "protected": tp.protected,
                "warnings": tp.warnings,
            }
            for tp in plan.teams
        ],
        "total_removals": plan.total_removals,
        "max_removals": plan.max_removals,
    }


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="entra-team-sync", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("plan", "show what would change (no writes)"),
        ("apply", "make the changes, then re-read each team to verify"),
        ("snapshot", "save the mapped Entra groups to a JSON file (no Semgrep calls)"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--config", required=True, help="mapping file (JSON)")
        p.add_argument("--entra-snapshot", help="read Entra groups from this snapshot file instead of Microsoft Graph")
        p.add_argument("--graph-page-size", type=int, default=999, help=argparse.SUPPRESS)  # testing: force paging
        if name != "snapshot":
            p.add_argument("--semgrep-url", default="https://semgrep.dev")
            p.add_argument("--json-out", help="also write the plan as JSON to this file")
        if name == "apply":
            p.add_argument("--allow-mass-removal", action="store_true", help="apply even if removals exceed max_removals")
        if name == "snapshot":
            p.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    try:
        config = load_config(Path(args.config))
        source = _entra_source(args)
        if args.command == "snapshot":
            groups = [source.group(gid) for gid in dict.fromkeys(m.entra_group_id for m in config.mappings)]
            Path(args.out).write_text(json.dumps(snapshot_to_json(groups), indent=2))
            print(f"wrote {len(groups)} group(s) to {args.out}")
            return 0
        api = SemgrepApi(_env("SEMGREP_API_TOKEN"), config.deployment_id, args.semgrep_url)
    except (ConfigError, OSError, KeyError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    plan = build_plan(config, api, source.group)
    _print_plan(plan)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(_plan_json(plan), indent=2))

    has_errors = any(t.error for t in plan.teams)
    if args.command == "plan":
        if plan.removal_guard_tripped:
            print(f"\nNote: apply would refuse: {plan.total_removals} removals > max_removals {plan.max_removals}.")
        return 1 if has_errors else 0

    if plan.removal_guard_tripped and not args.allow_mass_removal:
        print(
            f"\nRefusing to apply: {plan.total_removals} removals > max_removals {plan.max_removals}. "
            "Check the plan, then raise max_removals or pass --allow-mass-removal.",
            file=sys.stderr,
        )
        return 3

    result = apply_plan(plan, api)
    print(f"\nApplied: {len(result.applied)} team(s).")
    for f in result.failed:
        print(f"FAILED: {f}", file=sys.stderr)
    for d in result.drift:
        print(f"VERIFY MISMATCH: {d}", file=sys.stderr)
    if result.rejected:
        print(
            "Not added (Semgrep says they are not deployment members; check their SSO/SCIM account): "
            + ", ".join(sorted(e or str(u) for u, e in result.rejected.items())),
            file=sys.stderr,
        )
    return 1 if (has_errors or result.failed or result.drift or result.rejected) else 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
