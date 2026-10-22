# entra-semgrep-team-sync

Keeps Semgrep team membership in line with Microsoft Entra ID groups, without renaming groups or adding `[team:<role>]` suffixes.

- Each Entra group is assigned to a Semgrep team in a mapping file, by group ID.
- Group **members** become team **Member** and group **owners** become team **Manager**. Both roles can be changed per mapping.
- A team can be fed by several groups. When a user reaches a team through more than one, the highest role wins (Manager over Member over Read-only).

SCIM keeps handling joiners, leavers and deployment roles. This tool only manages team membership, using the public Semgrep Teams API.

## How a run works

1. Reads the users of each mapped group (nested groups included) and the group's owners from Microsoft Graph.
2. Finds each person's Semgrep account by email. By default it compares the Entra `mail` and `userPrincipalName` against the Semgrep account email, ignoring case.
3. Reads each team's current members and works out the differences.
4. `plan` prints those differences. `apply` makes them, then reads each team again to confirm the result.

Only actual differences are sent, so running it every few minutes is safe.

## Safety behaviour

- **Unprovisioned users:** people who don't have a Semgrep account yet are listed and skipped. They're added on a later run, once SCIM has created them.
- **Removal limit:** if a run would remove more than `max_removals` memberships across all teams, `apply` stops without writing anything (exit code 3).
- **Empty groups:** if a group returns nobody, that team gets additions only and no removals. An empty result is more often a permission or sync problem than a real change.
- **Unreadable groups:** if a group can't be read, its team is skipped entirely. Other teams still sync.
- **Teams it won't touch:** teams created by the `[team]` SCIM naming convention are refused, because Semgrep locks them. Default teams are refused too.
- **Protected users:** `protect_emails` lists users who are never removed, such as break-glass admins.
- **Additions only:** `remove_unlisted: false` makes the tool only add users and change roles. People added to a team by hand are then kept.

## Setup

### 1. Entra app registration

1. Register an app in Entra.
2. Under **API permissions → Microsoft Graph → Application**, add `GroupMember.Read.All` and `User.Read.All`.
3. Grant admin consent.
4. Create a client secret.

Owners only get a Semgrep account if they are also assigned to the Semgrep SCIM enterprise app. An owner who isn't assigned is reported as "not in Semgrep yet".

### 2. Semgrep API token

Create an API token with **Web API** access, as a Semgrep Admin, under Settings → Tokens. Project-level RBAC (teams) must be enabled on the deployment.

### 3. Mapping file

See [`config.example.json`](config.example.json).

| Field | Meaning |
|---|---|
| `deployment_id` | Numeric Semgrep deployment ID |
| `mappings[].entra_group_id` | Entra group object ID |
| `mappings[].team` | Semgrep team name, or numeric team ID |
| `mappings[].members_role` | Role for group members: `member` (default), `readonly`, `manager`, or `null` to ignore members |
| `mappings[].owners_role` | Role for group owners: `manager` (default), `member`, `readonly`, or `null` to ignore owners |
| `mappings[].create_team` | Create the team by name if it doesn't exist (default `false`) |
| `remove_unlisted` | Remove team members who aren't in any mapped group (default `true`) |
| `max_removals` | Removal limit per run (default `25`) |
| `protect_emails` | Users who are never removed |
| `match_attributes` | Entra attributes compared to the Semgrep email (default `["mail", "userPrincipalName"]`) |

### 4. Run

```bash
export SEMGREP_API_TOKEN=... AZURE_TENANT_ID=... AZURE_CLIENT_ID=... AZURE_CLIENT_SECRET=...
uv run entra-team-sync plan  --config config.json
uv run entra-team-sync apply --config config.json
```

The `plan` command never writes. Add `--json-out plan.json` for a machine-readable copy of the plan.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | A team error, a failed write, or a team that didn't verify after apply |
| 2 | Configuration or environment problem |
| 3 | Removal limit hit; nothing was written |

### Offline mode (no Graph calls)

`snapshot` saves the mapped groups to a JSON file. `--entra-snapshot <file>` makes `plan` and `apply` read groups from that file instead of from Graph. This is useful for reviewing changes, and for testing against a Semgrep deployment without Entra credentials. [`examples/snapshot.example.json`](examples/snapshot.example.json) shows the format.

### Scheduling

Run `apply` on a schedule, for example every 15–60 minutes, from any of these:
- an Azure Function or Container Apps job
- a GitHub Actions or Azure DevOps scheduled pipeline
- cron

Keep the secrets in the scheduler's secret store. Membership changes in Entra appear in Semgrep on the next run.

## Notes on the Semgrep API

These were verified against a live deployment on 2026-10-22.

| Call | Purpose |
|---|---|
| `POST /api/agent/deployments/{id}/users/list` | Maps email to user ID |
| `GET /api/permissions/v2/deployments/{id}/teams` | Lists teams |
| `POST /api/permissions/v2/deployments/{id}/teams` | Creates a team |
| `GET /api/permissions/v2/deployments/{id}/teams/{team_id}/members` | Lists a team's members |
| `PATCH /api/permissions/v2/deployments/{id}/teams/{team_id}` | Changes members (at most 500 per request) |

Behaviour the tool relies on:
- `UPDATE_ACTION_UPDATE` adds the user if they aren't on the team, and otherwise changes their role.
- `UPDATE_ACTION_REMOVE` must carry the member's **current** role. With any other role it returns 200 and does nothing.
- A single unknown user ID rejects the whole request, so the tool never sends one.
- The team's stored member count is updated from the requested actions, not the ones applied. That's why only real differences are sent.
- The `GET .../status` RBAC flag isn't reliable as a pre-check. The tool calls the Teams API directly.

## Development

```bash
uv sync
uv run pytest
```

The tests mock both APIs. The Semgrep mock (`tests/fakes.py`) reproduces the live behaviour listed above.
