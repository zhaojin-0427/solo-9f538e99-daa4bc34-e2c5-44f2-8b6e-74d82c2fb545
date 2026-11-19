"""Evidence package export/import: snapshot consistency, integrity checks,
atomic merge, idempotent retry, concurrency, and post-import rule continuity."""
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from conftest import handover, make_waybill, withdrawal


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def switch_db(monkeypatch, db_dir: Path):
    """Point the (module-global) app config at a fresh database directory."""
    db_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("APP_DB_PATH", str(db_dir / "chain.db"))
    monkeypatch.setenv("APP_DATA_DIR", str(db_dir))
    import importlib

    from app import config, database, main

    importlib.reload(config)
    importlib.reload(database)
    importlib.reload(main)
    database.init_db()
    return main


def fresh_client(monkeypatch, db_dir: Path):
    from fastapi.testclient import TestClient

    main = switch_db(monkeypatch, db_dir)
    return TestClient(main.app)


def entry(scan_id, revision, ts, giver, receiver):
    from app import schemas

    return schemas.ScanEntry(
        scan_id=scan_id, revision=revision, ts_utc=ts, giver=giver, receiver=receiver
    )


def tombstone(scan_id, revision):
    from app import schemas

    return schemas.ScanEntry(scan_id=scan_id, revision=revision, event_type="WITHDRAWAL")


def make_package(wid, origin, entries):
    """Build a self-consistent evidence package without touching any DB."""
    from app import schemas, services

    rows = [
        {
            "scan_id": e.scan_id,
            "revision": e.revision,
            "event_type": e.event_type,
            "ts_utc": schemas.to_utc_iso(e.ts_utc) if e.ts_utc is not None else None,
            "giver": e.giver,
            "receiver": e.receiver,
        }
        for e in entries
    ]
    chain = services.compute_chain_from_rows(wid, origin, rows)
    return schemas.EvidencePackage(
        waybill_id=wid,
        origin_party=origin,
        revisions=list(entries),
        chain=chain,
        digest=schemas.evidence_digest(wid, origin, list(entries)),
    )


def recompute_digest(pkg_json):
    from app import schemas

    pkg = schemas.EvidencePackage(**pkg_json)
    return schemas.evidence_digest(pkg.waybill_id, pkg.origin_party, pkg.revisions)


def recompute_chain(pkg_json):
    from app import schemas, services

    pkg = schemas.EvidencePackage(**pkg_json)
    rows = [
        {
            "scan_id": e.scan_id,
            "revision": e.revision,
            "event_type": e.event_type,
            "ts_utc": schemas.to_utc_iso(e.ts_utc) if e.ts_utc is not None else None,
            "giver": e.giver,
            "receiver": e.receiver,
        }
        for e in pkg.revisions
    ]
    return services.compute_chain_from_rows(pkg.waybill_id, pkg.origin_party, rows)


def db_revisions(client, wid):
    """(scan_id, revision) pairs currently stored, observed via a fresh export."""
    pkg = client.get(f"/api/waybills/{wid}/evidence").json()
    return {(r["scan_id"], r["revision"]) for r in pkg["revisions"]}


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #
def test_export_empty_waybill(client):
    make_waybill(client)
    r = client.get("/api/waybills/AWB-1/evidence")
    assert r.status_code == 200
    pkg = r.json()
    assert pkg["format"] == "custody-evidence/v1"
    assert pkg["waybill_id"] == "AWB-1"
    assert pkg["origin_party"] == "ORIG"
    assert pkg["revisions"] == []
    assert pkg["chain"]["chain_intact"] is True
    assert pkg["chain"]["current_custodian"] == "ORIG"
    assert pkg["digest"].startswith("sha256:")
    assert recompute_digest(pkg) == pkg["digest"]


def test_export_unknown_waybill_404(client):
    assert client.get("/api/waybills/NOPE/evidence").status_code == 404


def test_export_contains_full_revision_history_including_tombstones(client):
    make_waybill(client)
    client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={
            "scans": [
                handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
                handover("S2", 1, "2026-11-18T09:00:00Z", "B", "C"),
            ]
        },
    )
    # correction (superseded r1 of S1 stays in history) + withdrawal tombstone
    client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S1", 2, "2026-11-18T08:05:00Z", "ORIG", "B")]},
    )
    client.post(
        "/api/waybills/AWB-1/scans/batch", json={"scans": [withdrawal("S2", 2)]}
    )

    pkg = client.get("/api/waybills/AWB-1/evidence").json()
    got = {(r["scan_id"], r["revision"], r["event_type"]) for r in pkg["revisions"]}
    assert got == {
        ("S1", 1, "HANDOVER"),  # superseded but still exported
        ("S1", 2, "HANDOVER"),
        ("S2", 1, "HANDOVER"),  # withdrawn, still exported
        ("S2", 2, "WITHDRAWAL"),  # tombstone exported
    }
    # embedded chain equals the live query, and the digest is self-consistent
    live = client.get("/api/waybills/AWB-1/chain").json()
    assert pkg["chain"] == live
    assert recompute_digest(pkg) == pkg["digest"]
    assert recompute_chain(pkg).model_dump(mode="json") == pkg["chain"]


def test_export_reflects_only_committed_state_after_rollback(client):
    make_waybill(client)
    client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")]},
    )
    # conflicting batch is rolled back; export must not contain its revision
    client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={
            "scans": [
                handover("S2", 1, "2026-11-18T09:00:00Z", "B", "C"),
                handover("S1", 1, "2026-11-18T08:30:00Z", "ORIG", "X"),
            ]
        },
    )
    pkg = client.get("/api/waybills/AWB-1/evidence").json()
    assert db_revisions(client, "AWB-1") == {("S1", 1)}
    assert recompute_digest(pkg) == pkg["digest"]


def test_export_always_reads_one_committed_snapshot_under_concurrency(client):
    """Concurrent writer commits batches while exports run: every exported
    package must be internally consistent (digest + recomputed chain match)."""
    from app import config, database, schemas, services

    make_waybill(client)
    db_path = config.DB_PATH
    stop = threading.Event()
    errors: list[str] = []

    def writer() -> None:
        conn = database.connect(db_path)
        custodian = "ORIG"
        i = 0
        try:
            while not stop.is_set():
                i += 1
                receiver = f"P{i}"
                batch = schemas.BatchSubmit(
                    scans=[
                        schemas.ScanEntry(
                            scan_id=f"S{i}",
                            revision=1,
                            ts_utc=datetime(2026, 11, 18, 8, tzinfo=timezone.utc)
                            + timedelta(minutes=i),
                            giver=custodian,
                            receiver=receiver,
                        )
                    ]
                )
                services.apply_batch(conn, "AWB-1", batch)
                custodian = receiver
        except Exception as exc:  # pragma: no cover - failure signal
            errors.append(f"writer: {exc!r}")
        finally:
            conn.close()

    t = threading.Thread(target=writer)
    t.start()
    try:
        conn = database.connect(db_path)
        try:
            for _ in range(60):
                pkg = services.export_evidence(conn, "AWB-1")
                # digest must cover exactly the revisions in this package ...
                assert (
                    schemas.evidence_digest(pkg.waybill_id, pkg.origin_party, pkg.revisions)
                    == pkg.digest
                )
                # ... and the embedded chain must be recomputable from them
                rows = [
                    {
                        "scan_id": e.scan_id,
                        "revision": e.revision,
                        "event_type": e.event_type,
                        "ts_utc": schemas.to_utc_iso(e.ts_utc),
                        "giver": e.giver,
                        "receiver": e.receiver,
                    }
                    for e in pkg.revisions
                ]
                assert (
                    services.compute_chain_from_rows(
                        pkg.waybill_id, pkg.origin_party, rows
                    )
                    == pkg.chain
                )
                keys = [(e.scan_id, e.revision) for e in pkg.revisions]
                assert len(keys) == len(set(keys))
        finally:
            conn.close()
    finally:
        stop.set()
        t.join(timeout=30)
    assert not errors, errors


# --------------------------------------------------------------------------- #
# Import: happy path, merge semantics, idempotency
# --------------------------------------------------------------------------- #
def test_import_into_empty_db_roundtrip_and_retry(client, monkeypatch, tmp_path):
    make_waybill(client, "AWB-1", "ORIG")
    client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={
            "scans": [
                handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
                handover("S2", 1, "2026-11-18T09:00:00Z", "B", "C"),
            ]
        },
    )
    client.post(
        "/api/waybills/AWB-1/scans/batch", json={"scans": [withdrawal("S2", 2)]}
    )
    pkg = client.get("/api/waybills/AWB-1/evidence").json()

    with fresh_client(monkeypatch, tmp_path / "db2") as target:
        r = target.post("/api/evidence/import", json=pkg)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["imported_revisions"] == 3  # S1r1, S2r1, S2r2-tombstone
        assert body["already_present"] == 0
        assert body["digest"] == pkg["digest"]
        assert body["chain"] == pkg["chain"]

        # waybill was registered with the package's origin party
        waybills = target.get("/api/waybills").json()["waybills"]
        assert [(w["id"], w["origin_party"]) for w in waybills] == [("AWB-1", "ORIG")]

        # live query equals the merged chain
        assert target.get("/api/waybills/AWB-1/chain").json() == pkg["chain"]

        # retrying the very same package is a side-effect-free no-op
        r2 = target.post("/api/evidence/import", json=pkg)
        assert r2.status_code == 200
        body2 = r2.json()
        assert body2["imported_revisions"] == 0
        assert body2["already_present"] == 3
        assert body2["chain"] == body["chain"]
        assert db_revisions(target, "AWB-1") == {
            ("S1", 1),
            ("S2", 1),
            ("S2", 2),
        }

        # re-export from the target yields the identical deterministic digest
        pkg2 = target.get("/api/waybills/AWB-1/evidence").json()
        assert pkg2["digest"] == pkg["digest"]
        assert pkg2["chain"] == pkg["chain"]


def test_import_merges_only_missing_revisions(client, monkeypatch, tmp_path):
    make_waybill(client, "AWB-1", "ORIG")
    client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={
            "scans": [
                handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
                handover("S2", 1, "2026-11-18T09:00:00Z", "B", "C"),
            ]
        },
    )
    pkg = client.get("/api/waybills/AWB-1/evidence").json()

    with fresh_client(monkeypatch, tmp_path / "db2") as target:
        make_waybill(target, "AWB-1", "ORIG")
        # target already knows S1r1 (identical) and has its own later evidence:
        # a correction S2r2 and an extra scan S3 the package does not know about
        target.post(
            "/api/waybills/AWB-1/scans/batch",
            json={
                "scans": [
                    handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
                    handover("S2", 2, "2026-11-18T09:00:00Z", "B", "C"),
                    handover("S3", 1, "2026-11-18T10:00:00Z", "C", "D"),
                ]
            },
        )
        r = target.post("/api/evidence/import", json=pkg)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["already_present"] == 1  # S1r1 identical
        assert body["imported_revisions"] == 1  # S2r1 backfilled as history
        # union of both archives; target's higher revision still adopted
        assert db_revisions(target, "AWB-1") == {
            ("S1", 1),
            ("S2", 1),
            ("S2", 2),
            ("S3", 1),
        }
        chain = body["chain"]
        assert chain["chain_intact"] is True
        assert [s["scan_id"] for s in chain["verified_prefix"]] == ["S1", "S2", "S3"]
        assert chain["verified_prefix"][1]["revision"] == 2
        assert chain["current_custodian"] == "D"


def test_import_empty_package_registers_waybill(client):
    pkg = make_package("AWB-9", "ORIG", []).model_dump(mode="json")
    r = client.post("/api/evidence/import", json=pkg)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported_revisions"] == 0
    assert body["chain"]["current_custodian"] == "ORIG"
    assert client.get("/api/waybills/AWB-9/chain").status_code == 200


# --------------------------------------------------------------------------- #
# Import: integrity checks (corrupted packages -> 422, no writes at all)
# --------------------------------------------------------------------------- #
def test_import_tampered_revision_fails_digest(client):
    pkg = make_package(
        "AWB-1",
        "ORIG",
        [entry("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")],
    ).model_dump(mode="json")
    pkg["revisions"][0]["receiver"] = "TAMPERED"  # digest no longer matches
    r = client.post("/api/evidence/import", json=pkg)
    assert r.status_code == 422
    assert r.json()["error"] == "package_integrity"
    assert "digest" in r.json()["detail"]
    assert client.get("/api/waybills").json()["waybills"] == []


def test_import_forged_digest_rejected(client):
    pkg = make_package(
        "AWB-1",
        "ORIG",
        [entry("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")],
    ).model_dump(mode="json")
    pkg["digest"] = "sha256:" + "0" * 64
    r = client.post("/api/evidence/import", json=pkg)
    assert r.status_code == 422
    assert client.get("/api/waybills").json()["waybills"] == []


def test_import_tampered_chain_rejected_even_with_valid_digest(client):
    pkg = make_package(
        "AWB-1",
        "ORIG",
        [
            entry("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
            entry("S2", 1, "2026-11-18T09:00:00Z", "B", "C"),
        ],
    ).model_dump(mode="json")
    # digest does not cover the chain; only the chain recomputation catches this
    pkg["chain"]["current_custodian"] = "FORGED"
    r = client.post("/api/evidence/import", json=pkg)
    assert r.status_code == 422
    assert "chain" in r.json()["detail"]
    assert client.get("/api/waybills").json()["waybills"] == []


def test_import_duplicate_revision_key_rejected(client):
    pkg = make_package(
        "AWB-1",
        "ORIG",
        [entry("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")],
    ).model_dump(mode="json")
    pkg["revisions"].append(dict(pkg["revisions"][0]))  # same (scan_id, revision)
    pkg["digest"] = recompute_digest(pkg)  # even a consistent digest cannot save it
    r = client.post("/api/evidence/import", json=pkg)
    assert r.status_code == 422
    assert "duplicate" in r.json()["detail"]
    assert client.get("/api/waybills").json()["waybills"] == []


def test_import_unknown_format_rejected(client):
    pkg = make_package("AWB-1", "ORIG", []).model_dump(mode="json")
    pkg["format"] = "custody-evidence/v2"
    r = client.post("/api/evidence/import", json=pkg)
    assert r.status_code == 422


def test_import_malformed_digest_shape_rejected(client):
    pkg = make_package("AWB-1", "ORIG", []).model_dump(mode="json")
    pkg["digest"] = "not-a-digest"
    assert client.post("/api/evidence/import", json=pkg).status_code == 422


# --------------------------------------------------------------------------- #
# Import: merge conflicts -> 409, whole package rolled back
# --------------------------------------------------------------------------- #
def test_import_origin_mismatch_rejects_whole_package(client):
    make_waybill(client, "AWB-1", "OTHER-ORIGIN")
    pkg = make_package(
        "AWB-1",
        "ORIG",
        [entry("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")],
    ).model_dump(mode="json")
    r = client.post("/api/evidence/import", json=pkg)
    assert r.status_code == 409
    assert "origin_party" in r.json()["detail"]
    # nothing from the package was stored
    assert client.get("/api/waybills/AWB-1/evidence").json()["revisions"] == []


def test_import_same_revision_divergence_rolls_back_everything(client):
    make_waybill(client, "AWB-1", "ORIG")
    client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S2", 1, "2026-11-18T09:30:00Z", "B", "C")]},
    )
    # package: S1 (missing), S2r1 divergent (09:00 vs stored 09:30), S3 (missing)
    pkg = make_package(
        "AWB-1",
        "ORIG",
        [
            entry("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
            entry("S2", 1, "2026-11-18T09:00:00Z", "B", "C"),
            entry("S3", 1, "2026-11-18T10:00:00Z", "C", "D"),
        ],
    ).model_dump(mode="json")
    r = client.post("/api/evidence/import", json=pkg)
    assert r.status_code == 409
    assert "divergence" in r.json()["detail"]
    # no partial revisions: S1/S3 must NOT have been inserted, S2 untouched
    assert db_revisions(client, "AWB-1") == {("S2", 1)}
    chain = client.get("/api/waybills/AWB-1/chain").json()
    assert chain["unverified_scans"][0]["ts_utc"].startswith("2026-11-18T09:30")


# --------------------------------------------------------------------------- #
# Import: concurrency
# --------------------------------------------------------------------------- #
def test_concurrent_identical_imports_serialize_without_side_effects(client):
    from app import config, database, services

    pkg = make_package(
        "AWB-1",
        "ORIG",
        [
            entry("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
            entry("S2", 1, "2026-11-18T09:00:00Z", "B", "C"),
        ],
    )
    db_path = config.DB_PATH
    barrier = threading.Barrier(2)
    outcomes: list = []
    errors: list = []

    def importer() -> None:
        conn = database.connect(db_path)
        try:
            barrier.wait(timeout=10)
            resp = services.import_evidence(conn, pkg)
            outcomes.append((resp.imported_revisions, resp.already_present))
        except Exception as exc:  # pragma: no cover - failure signal
            errors.append(repr(exc))
        finally:
            conn.close()

    threads = [threading.Thread(target=importer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    # serialized: exactly one import wrote the revisions, the other was a no-op
    assert sorted(outcomes) == [(0, 2), (2, 0)]

    conn = database.connect(db_path)
    try:
        exported = services.export_evidence(conn, "AWB-1")
    finally:
        conn.close()
    assert exported.digest == pkg.digest
    assert exported.chain.chain_intact is True
    assert exported.chain.current_custodian == "C"


def test_concurrent_conflicting_imports_leave_no_partial_revisions(client):
    """Two valid-but-divergent packages race; the loser must contribute nothing."""
    from app import config, database, services

    p1 = make_package(
        "AWB-1",
        "ORIG",
        [
            entry("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
            entry("S2", 1, "2026-11-18T09:00:00Z", "B", "C"),
        ],
    )
    # divergent S1r1 listed LAST so the loser fails mid-merge, after inserts
    p2 = make_package(
        "AWB-1",
        "ORIG",
        [
            entry("S3", 1, "2026-11-18T09:30:00Z", "X", "Y"),
            entry("S1", 1, "2026-11-18T08:30:00Z", "ORIG", "X"),
        ],
    )
    db_path = config.DB_PATH
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def importer(name: str, pkg) -> None:
        conn = database.connect(db_path)
        try:
            barrier.wait(timeout=10)
            services.import_evidence(conn, pkg)
            results[name] = "ok"
        except services.DomainConflict:
            results[name] = "conflict"
        finally:
            conn.close()

    threads = [
        threading.Thread(target=importer, args=("p1", p1)),
        threading.Thread(target=importer, args=("p2", p2)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert sorted(results.values()) == ["conflict", "ok"]

    conn = database.connect(db_path)
    try:
        exported = services.export_evidence(conn, "AWB-1")
    finally:
        conn.close()
    # final state equals exactly one package, digest-verifiable; the loser's
    # unique revisions left no trace
    assert exported.digest in {p1.digest, p2.digest}
    got = {(e.scan_id, e.revision) for e in exported.revisions}
    assert got in ({("S1", 1), ("S2", 1)}, {("S1", 1), ("S3", 1)})


# --------------------------------------------------------------------------- #
# Post-import: the original rules still apply unchanged
# --------------------------------------------------------------------------- #
def test_imported_waybill_follows_all_normal_rules(client):
    pkg = make_package(
        "AWB-1",
        "ORIG",
        [
            entry("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
            entry("S2", 1, "2026-11-18T09:00:00Z", "B", "C"),
        ],
    ).model_dump(mode="json")
    r = client.post("/api/evidence/import", json=pkg)
    assert r.status_code == 200, r.text

    # identical resubmission of an imported revision is idempotent
    r = client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")]},
    )
    assert r.status_code == 200
    assert r.json()["idempotent_duplicates"] == 1
    assert r.json()["applied"] == 0

    # same-revision divergence against an imported revision -> 409
    r = client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "X")]},
    )
    assert r.status_code == 409

    # correction with a higher revision is adopted
    r = client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S1", 2, "2026-11-18T08:05:00Z", "ORIG", "B")]},
    )
    assert r.status_code == 200
    assert r.json()["chain"]["verified_prefix"][0]["revision"] == 2

    # stale revision below the adopted one -> 409
    r = client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")]},
    )
    assert r.status_code == 409
    assert "stale" in r.json()["detail"]

    # withdrawal of an imported scan
    r = client.post(
        "/api/waybills/AWB-1/scans/batch", json={"scans": [withdrawal("S2", 2)]}
    )
    assert r.status_code == 200
    assert r.json()["chain"]["current_custodian"] == "B"

    # late backfill re-reporting the withdrawn link under a new scan id
    r = client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S3", 1, "2026-11-18T09:30:00Z", "B", "D")]},
    )
    assert r.status_code == 200
    chain = r.json()["chain"]
    assert chain["chain_intact"] is True
    assert [s["scan_id"] for s in chain["verified_prefix"]] == ["S1", "S3"]
    assert chain["current_custodian"] == "D"

    # plain query still works and matches
    assert client.get("/api/waybills/AWB-1/chain").json() == chain
