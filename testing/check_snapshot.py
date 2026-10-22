"""Check a `snapshot` taken from the test tenant against what the fixtures should give.

Usage: python3 testing/check_snapshot.py testing/out/fixtures.json testing/out/snap.json
Exit 0 when every expectation holds.
"""

import json
import sys


def main(fixtures_path: str, snapshot_path: str) -> int:
    fx = json.load(open(fixtures_path))
    snap = json.load(open(snapshot_path))["groups"]
    name_by_id = {v: k for k, v in fx["users"].items()}
    failures, notes = [], []

    def names(users):
        return {name_by_id.get(u["id"], f"other:{u.get('userPrincipalName')}") for u in users}

    def expect(label, actual, wanted):
        if actual != wanted:
            failures.append(f"{label}: expected {sorted(wanted)}, got {sorted(actual)}")

    eng = snap.get(fx["groups"]["eng"])
    readers = snap.get(fx["groups"]["readers"])
    if eng is None or readers is None:
        print("FAIL: eng or readers group missing from snapshot")
        return 1

    # transitiveMembers: nested carol included, the eng-sub group itself filtered out
    expect("eng members", names(eng["members"]), {"bob", "erin", "carol"})
    eng_owners = names(eng["owners"])
    expect("eng owners (fixture users)", {n for n in eng_owners if not n.startswith("other:")}, {"alice", "carol", "dave"})
    extra = {n for n in eng_owners if n.startswith("other:")}
    if extra:
        notes.append(f"eng has extra owners (probably the admin who created it): {sorted(extra)}")
    expect("readers members", names(readers["members"]), {"alice", "frank"})

    erin = next((u for u in eng["members"] if u["id"] == fx["users"]["erin"]), None)
    if erin is None or erin.get("accountEnabled") is not False:
        failures.append("erin should be present with accountEnabled=false")

    alice = next((u for u in readers["members"] if u["id"] == fx["users"]["alice"]), {})
    if (alice.get("mail") or "").lower() != fx["semgrep_email"].lower():
        failures.append(f"alice.mail should be {fx['semgrep_email']}, got {alice.get('mail')}")
    bob = next((u for u in eng["members"] if u["id"] == fx["users"]["bob"]), {})
    if bob.get("mail"):
        notes.append(f"bob has mail={bob['mail']} (expected none for an unlicensed cloud user)")
    if not (bob.get("userPrincipalName") or "").lower().startswith("sgsync-bob@"):
        failures.append(f"bob.userPrincipalName unexpected: {bob.get('userPrincipalName')}")

    for n in notes:
        print(f"note: {n}")
    for f in failures:
        print(f"FAIL: {f}")
    print("OK: snapshot matches the fixtures" if not failures else f"{len(failures)} check(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
