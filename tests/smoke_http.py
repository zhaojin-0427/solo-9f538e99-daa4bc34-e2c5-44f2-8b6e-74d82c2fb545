"""End-to-end HTTP smoke test against a running uvicorn instance."""
import json
import os
import sys
import urllib.request

BASE = "http://127.0.0.1:" + os.environ.get("SMOKE_PORT", "8011")


def call(method, path, body=None, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = resp.read()
            return resp.status, payload if raw else json.loads(payload)
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload)
        except json.JSONDecodeError:
            return e.code, payload


def check(label, cond, extra=""):
    print(("PASS" if cond else "FAIL"), label, extra)
    if not cond:
        sys.exit(1)


st, body = call("POST", "/api/waybills", {"waybill_id": "AWB-SMOKE", "origin_party": "ORIG"})
check("create waybill", st == 201, body)

st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S1", "revision": 1, "ts_utc": "2026-11-18T08:00:00Z", "giver": "ORIG", "receiver": "B"},
    {"scan_id": "S2", "revision": 1, "ts_utc": "2026-11-18T09:00:00Z", "giver": "B", "receiver": "C"},
    {"scan_id": "S3", "revision": 1, "ts_utc": "2026-11-18T10:00:00Z", "giver": "X", "receiver": "Y"},
]})
check("batch accepted", st == 200, st)
check("GAP detected", body["chain"]["break_reason"] == "GAP")
check("custodian stays at C", body["chain"]["current_custodian"] == "C")
check("S3 unverified", [s["scan_id"] for s in body["chain"]["unverified_scans"]] == ["S3"])

# same revision divergence -> 409, old chain preserved
st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S3", "revision": 1, "ts_utc": "2026-11-18T10:00:00Z", "giver": "X", "receiver": "TAMPERED"},
]})
check("same-revision divergence -> 409", st == 409, body)

st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S3", "revision": 2, "event_type": "WITHDRAWAL"},
]})
check("withdrawal accepted", st == 200, body)
st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S3", "revision": 3, "ts_utc": "2026-11-18T10:00:00Z", "giver": "C", "receiver": "D"},
]})
check("correction accepted", st == 200, st)
check("chain healed", body["chain"]["chain_intact"] is True)
check("custodian now D", body["chain"]["current_custodian"] == "D")

# simultaneous timestamps
st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S4", "revision": 1, "ts_utc": "2026-11-18T11:00:00Z", "giver": "D", "receiver": "E"},
    {"scan_id": "S5", "revision": 1, "ts_utc": "2026-11-18T11:00:00Z", "giver": "E", "receiver": "F"},
]})
check("simultaneous flagged", body["chain"]["break_reason"] == "SIMULTANEOUS")

st, body = call("GET", "/api/waybills/AWB-SMOKE/chain")
check("GET chain", st == 200 and body["waybill_id"] == "AWB-SMOKE")

st, body = call("GET", "/api/waybills")
check("list waybills", st == 200 and any(w["id"] == "AWB-SMOKE" for w in body["waybills"]))

# --- evidence package export / import -------------------------------------
st, pkg = call("GET", "/api/waybills/AWB-SMOKE/evidence")
check("export evidence", st == 200 and pkg["digest"].startswith("sha256:"), pkg.get("digest"))
check("export format", pkg["format"] == "custody-evidence/v1")
check("export origin", pkg["origin_party"] == "ORIG")
# full history: S1r1, S2r1, S3r1, S3r2(W), S3r3, S4r1, S5r1
check("export carries all revisions", len(pkg["revisions"]) == 7, len(pkg["revisions"]))
check("export embeds chain", pkg["chain"]["break_reason"] == "SIMULTANEOUS")

# re-import into the same archive: fully idempotent no-op, merged chain returned
st, body = call("POST", "/api/evidence/import", pkg)
check("re-import idempotent", st == 200 and body["imported_revisions"] == 0
      and body["already_present"] == 7, body)
check("merged chain returned", body["chain"]["waybill_id"] == "AWB-SMOKE"
      and body["chain"]["break_reason"] == "SIMULTANEOUS")

# tampered package -> 422, archive untouched
import copy
bad = copy.deepcopy(pkg)
bad["revisions"][0]["receiver"] = "TAMPERED"
st, body = call("POST", "/api/evidence/import", bad)
check("tampered package -> 422", st == 422 and body["error"] == "package_integrity", body)
bad2 = copy.deepcopy(pkg)
bad2["chain"]["current_custodian"] = "FORGED"
st, body = call("POST", "/api/evidence/import", bad2)
check("forged chain -> 422", st == 422, body)
st, body = call("GET", "/api/waybills/AWB-SMOKE/chain")
check("chain unchanged after bad imports", st == 200 and body["current_custodian"] == "D")

# origin mismatch on an existing waybill -> 409
bad3 = copy.deepcopy(pkg)
bad3["origin_party"] = "OTHER"
st, body = call("POST", "/api/evidence/import", bad3)
check("origin mismatch -> 422/409", st in (409, 422), st)

st, body = call("GET", "/docs", raw=True)
check("swagger docs reachable", st == 200 and b"swagger" in body.lower())
st, body = call("GET", "/openapi.json")
check("openapi schema reachable", st == 200 and "paths" in body)
st, body = call("GET", "/", raw=True)
check("console page reachable", st == 200 and b"<!DOCTYPE html>" in body)

# validation: naive datetime
st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S9", "revision": 1, "ts_utc": "2026-11-18T12:00:00", "giver": "D", "receiver": "E"},
]})
check("naive timestamp -> 422", st == 422)

print("ALL SMOKE CHECKS PASSED")
