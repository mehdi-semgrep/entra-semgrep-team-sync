#!/usr/bin/env bash
# Delete what setup_entra_fixtures.sh created, and nothing else: only the object
# ids recorded in testing/out/created.json, in the tenant recorded there. Entra keeps
# deleted users, groups and apps in its recycle bin for 30 days.
set -euo pipefail

OUT="$(cd "$(dirname "$0")" && pwd)/out"
LEDGER="$OUT/created.json"
[[ -f "$LEDGER" ]] || { echo "no $LEDGER; nothing to tear down" >&2; exit 1; }

ledger() { # tenant_id | app | users | groups -> value(s)
  python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
v = d[sys.argv[2]]
print(" ".join(v.values()) if isinstance(v, dict) else (v or ""))' "$LEDGER" "$1"
}

TENANT_ID="$(az account show --query tenantId -o tsv)"
[[ "$TENANT_ID" == "$(ledger tenant_id)" ]] \
  || { echo "signed in to tenant $TENANT_ID, but $LEDGER is for $(ledger tenant_id)" >&2; exit 1; }

for gid in $(ledger groups); do
  az ad group delete --group "$gid" && echo "deleted group $gid"
done
for uid in $(ledger users); do
  az ad user delete --id "$uid" && echo "deleted user $uid"
done
APP_ID="$(ledger app)"
if [[ -n "$APP_ID" ]]; then
  az ad app delete --id "$APP_ID" && echo "deleted app $APP_ID"
  security delete-generic-password -s entra-team-sync-test-secret -a "$APP_ID" >/dev/null 2>&1 || true
fi
rm -f "$LEDGER" "$OUT/fixtures.json" "$OUT/config.level1.json" "$OUT/snap.json"
echo "done"
