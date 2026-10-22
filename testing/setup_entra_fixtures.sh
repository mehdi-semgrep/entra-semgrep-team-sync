#!/usr/bin/env bash
# Create test users, groups and an app registration in an Entra TEST tenant.
#
# Usage: testing/setup_entra_fixtures.sh <semgrep-email> <deployment-id>
#   <semgrep-email>  email of an existing Semgrep account on the test deployment;
#                    it becomes the `mail` of the test user sgsync-alice so that one
#                    user is matched end to end. Everyone else stays unmatched.
#
# Needs: az CLI signed in to the test tenant as a user who can create users,
# groups and app registrations and grant admin consent:
#   az login --tenant <tenant-id> --allow-no-subscriptions
#
# Ownership: every object this script creates is recorded in testing/out/created.json
# as soon as it exists. A re-run reuses only those objects. If an object with a
# fixture name exists but is not in that record, the script stops instead of
# touching it, and teardown only ever deletes what the record lists.
#
# Writes testing/out/fixtures.json and testing/out/config.level1.json.
# The app's client secret goes into the macOS keychain, never to stdout or disk.
set -euo pipefail

SEMGREP_EMAIL="${1:?usage: $0 <semgrep-email> <deployment-id>}"
DEPLOYMENT_ID="${2:?usage: $0 <semgrep-email> <deployment-id>}"
PREFIX="sgsync"
APP_NAME="${PREFIX}-entra-team-sync-test"
KEYCHAIN_SERVICE="entra-team-sync-test-secret"
GRAPH_API="00000003-0000-0000-c000-000000000000"
# Microsoft Graph application permissions (app role ids).
GROUPMEMBER_READ_ALL="98830695-27a2-44f7-8c18-0c3ebc9698f6"
USER_READ_ALL="df021288-bdef-4463-88db-98f22de89214"

OUT="$(cd "$(dirname "$0")" && pwd)/out"
LEDGER="$OUT/created.json"
mkdir -p "$OUT"

die() { echo "error: $*" >&2; exit 1; }

TENANT_ID="$(az account show --query tenantId -o tsv)"
DOMAIN="$(az rest --method get --url 'https://graph.microsoft.com/v1.0/domains' \
  --query 'value[?isDefault].id | [0]' -o tsv)"
echo "tenant ${TENANT_ID}, domain ${DOMAIN}"

[[ -f "$LEDGER" ]] || echo "{\"tenant_id\": \"${TENANT_ID}\", \"users\": {}, \"groups\": {}, \"app\": null}" >"$LEDGER"

ledger_get() { # kind name -> recorded id, or empty
  python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
v = d["app"] if sys.argv[2] == "app" else d[sys.argv[2]].get(sys.argv[3])
print(v or "")' "$LEDGER" "$1" "${2:-}"
}
ledger_put() { # kind name id
  python3 -c '
import json, sys
path, kind, name, oid = sys.argv[1:5]
d = json.load(open(path))
if kind == "app":
    d["app"] = oid
else:
    d[kind][name] = oid
json.dump(d, open(path, "w"), indent=2)' "$LEDGER" "$1" "$2" "$3"
}

[[ "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["tenant_id"])' "$LEDGER")" == "$TENANT_ID" ]] \
  || die "$LEDGER belongs to another tenant; run teardown there or remove it"

user() { # name -> object id
  local upn="${PREFIX}-$1@${DOMAIN}" id recorded
  recorded="$(ledger_get users "$1")"
  id="$(az ad user show --id "$upn" --query id -o tsv 2>/dev/null || true)"
  if [[ -n "$id" ]]; then
    [[ "$id" == "$recorded" ]] || die "user $upn already exists and was not created by this script"
  else
    # Random password, never stored: these users never need to sign in.
    id="$(az ad user create --display-name "${PREFIX} $1" --user-principal-name "$upn" \
      --password "$(openssl rand -base64 24)Aa1!" --force-change-password-next-sign-in true \
      --query id -o tsv)"
    ledger_put users "$1" "$id"
  fi
  echo "$id"
}

group() { # name -> object id
  local name="${PREFIX}-$1" ids recorded
  recorded="$(ledger_get groups "$1")"
  ids="$(az ad group list --display-name "$name" --query '[].id' -o tsv)"
  if [[ -n "$ids" ]]; then
    [[ "$ids" == "$recorded" ]] || die "group $name already exists and was not created by this script"
    echo "$ids"
    return
  fi
  ids="$(az ad group create --display-name "$name" --mail-nickname "$name" --query id -o tsv)"
  ledger_put groups "$1" "$ids"
  echo "$ids"
}

add_member() { az ad group member check --group "$1" --member-id "$2" --query value -o tsv | grep -q true \
  || az ad group member add --group "$1" --member-id "$2"; }
add_owner() { az ad group owner list --group "$1" --query "[?id=='$2'] | length(@)" -o tsv | grep -q '^1$' \
  || az ad group owner add --group "$1" --owner-object-id "$2"; }

echo "users..."
ALICE="$(user alice)"; BOB="$(user bob)"; CAROL="$(user carol)"
DAVE="$(user dave)"; ERIN="$(user erin)"; FRANK="$(user frank)"

az ad user update --id "$ERIN" --account-enabled false
if ! az rest --method patch --url "https://graph.microsoft.com/v1.0/users/${ALICE}" \
  --headers Content-Type=application/json --body "{\"mail\": \"${SEMGREP_EMAIL}\"}" >/dev/null; then
  echo "WARNING: could not set mail on sgsync-alice; she will stay unmatched" >&2
fi

echo "groups..."
ENG="$(group eng)"; ENG_SUB="$(group eng-sub)"; READERS="$(group readers)"
add_member "$ENG" "$BOB"
add_member "$ENG" "$ERIN"          # disabled: must be skipped
add_member "$ENG" "$ENG_SUB"       # nested group
add_member "$ENG_SUB" "$CAROL"     # member of eng only through nesting
add_owner "$ENG" "$ALICE"          # owner -> manager
add_owner "$ENG" "$CAROL"          # owner and (nested) member -> manager
add_owner "$ENG" "$DAVE"           # owner who is not a member -> manager
add_member "$READERS" "$ALICE"     # readonly here, but manager via eng: highest wins
add_member "$READERS" "$FRANK"

echo "app registration..."
RECORDED_APP="$(ledger_get app)"
EXISTING_APPS="$(az ad app list --display-name "$APP_NAME" --query '[].appId' -o tsv)"
if [[ -n "$EXISTING_APPS" ]]; then
  [[ "$EXISTING_APPS" == "$RECORDED_APP" ]] || die "app $APP_NAME already exists and was not created by this script"
  APP_ID="$RECORDED_APP"
else
  APP_ID="$(az ad app create --display-name "$APP_NAME" --sign-in-audience AzureADMyOrg --query appId -o tsv)"
  ledger_put app "" "$APP_ID"
fi
az ad sp show --id "$APP_ID" >/dev/null 2>&1 || az ad sp create --id "$APP_ID" >/dev/null
az ad app permission add --id "$APP_ID" --api "$GRAPH_API" \
  --api-permissions "${GROUPMEMBER_READ_ALL}=Role" "${USER_READ_ALL}=Role" 2>/dev/null || true

# Consent covers everything the app requests, so check it requests exactly the two
# read permissions and nothing else before granting it.
az ad app show --id "$APP_ID" --query requiredResourceAccess -o json | python3 -c '
import json, sys
want = {(sys.argv[1], sys.argv[2], "Role"), (sys.argv[1], sys.argv[3], "Role")}
got = {(r["resourceAppId"], a["id"], a["type"]) for r in json.load(sys.stdin) for a in r["resourceAccess"]}
if got != want:
    sys.exit(f"error: app requests {sorted(got)}, expected only {sorted(want)}; not granting consent")
' "$GRAPH_API" "$GROUPMEMBER_READ_ALL" "$USER_READ_ALL"

CONSENTED=false
for attempt in 1 2 3 4 5; do  # the new service principal can take a moment to appear
  if az ad app permission admin-consent --id "$APP_ID"; then
    CONSENTED=true
    break
  fi
  sleep $((attempt * 10))
done
$CONSENTED || die "admin consent failed after 5 attempts (does this account have consent rights?)"

if ! security find-generic-password -s "$KEYCHAIN_SERVICE" -a "$APP_ID" >/dev/null 2>&1; then
  SECRET="$(az ad app credential reset --id "$APP_ID" --display-name "${PREFIX}-test" --years 1 \
    --append --query password -o tsv)"
  security add-generic-password -s "$KEYCHAIN_SERVICE" -a "$APP_ID" -w "$SECRET"
  unset SECRET
fi

cat >"$OUT/fixtures.json" <<EOF
{
  "tenant_id": "${TENANT_ID}",
  "client_id": "${APP_ID}",
  "domain": "${DOMAIN}",
  "semgrep_email": "${SEMGREP_EMAIL}",
  "groups": {"eng": "${ENG}", "eng-sub": "${ENG_SUB}", "readers": "${READERS}"},
  "users": {"alice": "${ALICE}", "bob": "${BOB}", "carol": "${CAROL}",
            "dave": "${DAVE}", "erin": "${ERIN}", "frank": "${FRANK}"}
}
EOF

cat >"$OUT/config.level1.json" <<EOF
{
  "deployment_id": ${DEPLOYMENT_ID},
  "max_removals": 1,
  "mappings": [
    {"entra_group_id": "${ENG}", "team": "${PREFIX}-probe", "create_team": true},
    {"entra_group_id": "${READERS}", "team": "${PREFIX}-probe", "members_role": "readonly", "owners_role": null}
  ]
}
EOF

echo "done: $OUT/fixtures.json, $OUT/config.level1.json"
echo "client secret is in keychain service '${KEYCHAIN_SERVICE}', account '${APP_ID}'"
