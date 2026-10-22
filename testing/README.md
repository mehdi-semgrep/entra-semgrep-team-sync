# Testing against a real Entra tenant

Use a **test** tenant only.

- The setup script creates users, groups and an app registration, all prefixed `sgsync-`. It records each object in `testing/out/created.json` as soon as it creates it.
- A re-run reuses only recorded objects. If a `sgsync-` object exists that setup didn't create, it stops instead of touching it.
- Before granting admin consent, setup checks the app requests exactly the two read permissions, and it stops if consent fails.
- The teardown script deletes only the recorded objects, and only in the tenant they were created in.
- Entra keeps deleted objects in its recycle bin for 30 days.

## Prerequisites

- The Azure CLI (`brew install azure-cli`), signed in to the test tenant. The account needs rights to create users, groups and app registrations, and to grant admin consent; Global Administrator, or User + Groups + Application Administrator.
  ```bash
  az login --tenant <tenant-id> --allow-no-subscriptions
  ```
- A Semgrep test deployment with teams (RBAC) enabled, and a Web API token for it:
  ```bash
  export SEMGREP_API_TOKEN=...
  ```

## Level 1: the Graph side, against real Entra data

### 1. Create the test data

```bash
testing/setup_entra_fixtures.sh <your-semgrep-email> <deployment-id>
```

What it builds:

| Entra object | Contents | Purpose |
|---|---|---|
| `sgsync-eng` | members `bob`, `erin` (disabled), nested group `sgsync-eng-sub` (`carol`); owners `alice`, `carol`, `dave` | nesting, owners, disabled users |
| `sgsync-readers` | members `alice`, `frank` | second group feeding the same team as Read-only |
| `sgsync-alice` | `mail` set to your Semgrep email | the only user that matches a Semgrep account |
| App `sgsync-entra-team-sync-test` | Graph `GroupMember.Read.All` + `User.Read.All`, admin consent | the app the script logs in as; its secret goes into the keychain |

### 2. Read Entra only, then check the result

```bash
export AZURE_TENANT_ID=$(python3 -c "import json;print(json.load(open('testing/out/fixtures.json'))['tenant_id'])")
export AZURE_CLIENT_ID=$(python3 -c "import json;print(json.load(open('testing/out/fixtures.json'))['client_id'])")
export AZURE_CLIENT_SECRET=$(security find-generic-password -s entra-team-sync-test-secret -a "$AZURE_CLIENT_ID" -w)
uv run entra-team-sync snapshot --config testing/out/config.level1.json --out testing/out/snap.json
python3 testing/check_snapshot.py testing/out/fixtures.json testing/out/snap.json
```

A new app secret can take a minute or two to start working. If the first call returns 401, wait and retry.

### 3. Plan against Semgrep

```bash
uv run entra-team-sync plan --config testing/out/config.level1.json
```

Expected output:

```
== team 'sgsync-probe' [will be created]
   + add     <your-semgrep-email> as manager    <- owner of eng beats readonly from readers
   not in Semgrep yet (skipped): sgsync-bob@..., sgsync-carol@..., sgsync-dave@..., sgsync-frank@...
```

`erin` must **not** appear anywhere, because she's disabled.

### 4. Apply, then re-run

```bash
uv run entra-team-sync apply --config testing/out/config.level1.json
uv run entra-team-sync apply --config testing/out/config.level1.json
```

The second run must show `no changes`.

### 5. Change something in Entra and run `apply` again

| Change in Entra | Expected next run |
|---|---|
| Remove `alice` as owner of `sgsync-eng` | `~ change ... manager -> readonly` (she's still in readers) |
| Remove `alice` from `sgsync-readers` as well | `- remove ...`; the removal limit is 1, so it applies |

### 6. Clean up

Delete the `sgsync-probe` team in Semgrep (UI, or `DELETE` on the API), then run:

```bash
testing/teardown_entra_fixtures.sh
```

## Level 2: with SCIM provisioning

This level answers what Level 1 can't:
- which Entra attribute SCIM uses for the Semgrep email
- whether users SCIM created, who have never logged in, can be added to teams

1. Turn on SCIM for the test deployment (Settings → SSO/SCIM) and complete the WorkOS setup for Entra. The deployment needs the enterprise SSO entitlement; ask Platform Services if the option isn't there.
2. In the Semgrep enterprise app in Entra, assign `sgsync-eng`, `sgsync-eng-sub` and `sgsync-readers`, then provision. Don't add a `[team]` suffix, and don't sign in as the test users.
3. Remove the `mail` override from `alice`, so matching relies on what SCIM actually sends.
4. Run `plan`. Expected: `bob` as member, `alice`, `carol` and `dave` as manager, `frank` as readonly, and nobody listed as "not in Semgrep yet".
   - `dave` is only an owner. Owners are not provisioned unless they're assigned to the app, so assign `dave` directly to check that path.
5. Run `apply`, change memberships in Entra, and run `apply` again (as in Level 1).
6. **SCIM-lock check:** create and assign a group named `sgsync-probe [team]`. The next `plan` must report the team as managed by SCIM and leave it alone.
