"""Domain logic: revision arbitration, atomic batch apply, chain recomputation.

All write paths run inside one ``BEGIN IMMEDIATE`` transaction.  Any rule
violation raises ``DomainConflict`` / ``DomainValidation`` and rolls the whole
batch back, so the previously committed chain is preserved untouched.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .schemas import (
    EVIDENCE_FORMAT,
    BatchResponse,
    BatchSubmit,
    BreakReason,
    ChainResponse,
    EvidenceImportResponse,
    EvidencePackage,
    ScanEntry,
    ScanView,
    WaybillCreate,
    entry_payload_hash,
    evidence_digest,
    to_utc_iso,
    utcnow_iso,
)

_STORAGE_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


class DomainConflict(Exception):
    """HTTP 409: same-revision divergence, stale revision, unknown scan ..."""


class DomainValidation(Exception):
    """HTTP 404: referenced waybill/resource does not exist."""


class PackageIntegrity(Exception):
    """HTTP 422: evidence package failed digest / uniqueness / chain checks."""


# --------------------------------------------------------------------------- #
# Waybills
# --------------------------------------------------------------------------- #
def create_waybill(conn: sqlite3.Connection, body: WaybillCreate) -> dict:
    from .database import transaction

    with transaction(conn):
        existing = conn.execute(
            "SELECT id FROM waybills WHERE id = ?", (body.waybill_id,)
        ).fetchone()
        if existing is not None:
            raise DomainConflict(f"waybill {body.waybill_id!r} already exists")
        conn.execute(
            "INSERT INTO waybills (id, origin_party, created_at) VALUES (?, ?, ?)",
            (body.waybill_id, body.origin_party, utcnow_iso()),
        )
    return {"waybill_id": body.waybill_id, "origin_party": body.origin_party}


def get_waybill(conn: sqlite3.Connection, waybill_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, origin_party FROM waybills WHERE id = ?", (waybill_id,)
    ).fetchone()
    if row is None:
        raise DomainValidation(f"waybill {waybill_id!r} not found")
    return row


# --------------------------------------------------------------------------- #
# Batch submission (handover reports, corrections, withdrawals)
# --------------------------------------------------------------------------- #
def apply_batch(
    conn: sqlite3.Connection, waybill_id: str, batch: BatchSubmit
) -> BatchResponse:
    from .database import transaction

    get_waybill(conn, waybill_id)  # 404-style check before opening the txn

    applied = 0
    idempotent = 0
    withdrawn: list[str] = []

    with transaction(conn):
        for entry in batch.scans:
            wrote = _apply_one(conn, waybill_id, entry)
            if not wrote:
                idempotent += 1
                continue
            if entry.event_type == "WITHDRAWAL":
                withdrawn.append(entry.scan_id)
            applied += 1

    # Recompute against the freshly committed snapshot.  Scan writes and the
    # returned query result therefore become visible together.
    chain = compute_chain(conn, waybill_id)
    return BatchResponse(
        waybill_id=waybill_id,
        applied=applied,
        idempotent_duplicates=idempotent,
        withdrawn_scan_ids=withdrawn,
        chain=chain,
    )


def _apply_one(conn: sqlite3.Connection, waybill_id: str, entry: ScanEntry) -> bool:
    """Insert one scan revision.  Returns False when it was an idempotent no-op."""
    h = entry_payload_hash(entry)

    newest = conn.execute(
        "SELECT revision, event_type, payload_hash "
        "FROM scans WHERE waybill_id = ? AND scan_id = ? "
        "ORDER BY revision DESC LIMIT 1",
        (waybill_id, entry.scan_id),
    ).fetchone()

    if newest is not None and newest["revision"] > entry.revision:
        raise DomainConflict(
            f"scan_id={entry.scan_id!r} revision={entry.revision} is stale: "
            f"revision {newest['revision']} already adopted"
        )

    same_rev = conn.execute(
        "SELECT event_type, payload_hash FROM scans "
        "WHERE waybill_id = ? AND scan_id = ? AND revision = ?",
        (waybill_id, entry.scan_id, entry.revision),
    ).fetchone()

    if same_rev is not None:
        # 同修订异文 -> 整批拒绝；同文 -> 幂等跳过
        if same_rev["payload_hash"] != h:
            raise DomainConflict(
                f"scan_id={entry.scan_id!r} revision={entry.revision} already "
                "exists with different content (same-revision divergence)"
            )
        return False

    if entry.event_type == "WITHDRAWAL" and newest is None:
        raise DomainConflict(
            f"cannot withdraw unknown scan_id={entry.scan_id!r}: no prior revision exists"
        )

    _insert_revision(conn, waybill_id, entry, h)
    return True


def _insert_revision(
    conn: sqlite3.Connection, waybill_id: str, entry: ScanEntry, payload_hash: str
) -> None:
    ts_text = to_utc_iso(entry.ts_utc) if entry.ts_utc is not None else None
    conn.execute(
        "INSERT INTO scans (waybill_id, scan_id, revision, event_type, "
        "ts_utc, giver, receiver, payload_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            waybill_id,
            entry.scan_id,
            entry.revision,
            entry.event_type,
            ts_text,
            entry.giver,
            entry.receiver,
            payload_hash,
            utcnow_iso(),
        ),
    )


# --------------------------------------------------------------------------- #
# Chain recomputation
# --------------------------------------------------------------------------- #
def _fetch_revision_rows(conn: sqlite3.Connection, waybill_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT scan_id, revision, event_type, ts_utc, giver, receiver "
        "FROM scans WHERE waybill_id = ? ORDER BY scan_id ASC, revision ASC",
        (waybill_id,),
    ).fetchall()


def _adopt_handovers(rows: list) -> list:
    """Keep each scan's highest revision; drop withdrawal tombstones; time-order.

    ``rows`` are mappings (sqlite3.Row or dict) with canonical ``ts_utc``
    strings, so lexicographic ordering equals chronological ordering.
    """
    newest: dict[str, object] = {}
    for row in rows:
        current = newest.get(row["scan_id"])
        if current is None or row["revision"] > current["revision"]:
            newest[row["scan_id"]] = row
    adopted = [r for r in newest.values() if r["event_type"] == "HANDOVER"]
    adopted.sort(key=lambda r: (r["ts_utc"], r["scan_id"]))
    return adopted


def _parse_ts(text: str) -> datetime:
    return datetime.strptime(text, _STORAGE_FMT).replace(tzinfo=timezone.utc)


def compute_chain_from_rows(
    waybill_id: str, origin: str, rows: list
) -> ChainResponse:
    """Pure chain recomputation over revision rows (DB rows or package rows)."""
    adopted = _adopt_handovers(rows)

    # Group by timestamp: simultaneous handovers cannot be ordered by guessing.
    groups: list[tuple[str, list]] = []
    for row in adopted:
        if groups and groups[-1][0] == row["ts_utc"]:
            groups[-1][1].append(row)
        else:
            groups.append((row["ts_utc"], [row]))

    verified: list[ScanView] = []
    unverified: list[ScanView] = []
    custodian = origin
    broken = False
    break_reason = BreakReason.none
    break_detail: str | None = None
    break_scan_ids: list[str] = []

    def view(row, ok: bool, reason: BreakReason | None) -> ScanView:
        return ScanView(
            scan_id=row["scan_id"],
            revision=row["revision"],
            event_type=row["event_type"],
            ts_utc=_parse_ts(row["ts_utc"]),
            giver=row["giver"],
            receiver=row["receiver"],
            verified=ok,
            unverified_reason=reason,
        )

    for ts, group in groups:
        if broken:
            # 断链之后的扫描一律未证实，直到补报/更正/撤回补齐缺口
            unverified.extend(view(r, False, break_reason) for r in group)
            continue

        if len(group) > 1:
            broken = True
            break_reason = BreakReason.simultaneous
            break_scan_ids = [r["scan_id"] for r in group]
            break_detail = (
                f"{len(group)} handovers share ts_utc={ts}; order cannot be "
                "determined, so none of them is verified"
            )
            unverified.extend(view(r, False, BreakReason.simultaneous) for r in group)
            continue

        scan = group[0]
        if scan["giver"] != custodian:
            broken = True
            break_reason = BreakReason.gap
            break_scan_ids = [scan["scan_id"]]
            break_detail = (
                f"giver {scan['giver']!r} does not hold custody; expected "
                f"{custodian!r} (previous receiver / origin party)"
            )
            unverified.append(view(scan, False, BreakReason.gap))
            continue

        verified.append(view(scan, True, None))
        custodian = scan["receiver"]

    return ChainResponse(
        waybill_id=waybill_id,
        origin_party=origin,
        current_custodian=custodian,
        verified_prefix=verified,
        unverified_scans=unverified,
        chain_intact=not broken,
        break_reason=break_reason,
        break_detail=break_detail,
        break_scan_ids=break_scan_ids,
    )


def compute_chain(conn: sqlite3.Connection, waybill_id: str) -> ChainResponse:
    waybill = get_waybill(conn, waybill_id)
    rows = _fetch_revision_rows(conn, waybill_id)
    return compute_chain_from_rows(waybill_id, waybill["origin_party"], rows)


# --------------------------------------------------------------------------- #
# Evidence packages: export from / import into this archive
# --------------------------------------------------------------------------- #
def _canonical_row(entry: ScanEntry) -> dict:
    """Row-shaped canonical form of a package revision (mirrors DB storage)."""
    return {
        "scan_id": entry.scan_id,
        "revision": entry.revision,
        "event_type": entry.event_type,
        "ts_utc": to_utc_iso(entry.ts_utc) if entry.ts_utc is not None else None,
        "giver": entry.giver,
        "receiver": entry.receiver,
    }


def export_evidence(conn: sqlite3.Connection, waybill_id: str) -> EvidencePackage:
    """导出证据包：运单号、起始保管方、全部修订（含撤回）、可信链与确定性摘要。

    运单行、全部修订行与可信链都在同一个只读事务快照内读取（WAL 下
    不阻塞写入），因此包内容永远对应同一个已提交状态，绝不混入
    并发批次的一半。
    """
    conn.execute("BEGIN;")  # deferred read transaction: pins one committed snapshot
    try:
        waybill = get_waybill(conn, waybill_id)
        rows = _fetch_revision_rows(conn, waybill_id)
        chain = compute_chain_from_rows(waybill_id, waybill["origin_party"], rows)
    finally:
        conn.rollback()

    revisions = [
        ScanEntry(
            scan_id=r["scan_id"],
            revision=r["revision"],
            event_type=r["event_type"],
            ts_utc=_parse_ts(r["ts_utc"]) if r["ts_utc"] is not None else None,
            giver=r["giver"],
            receiver=r["receiver"],
        )
        for r in rows
    ]
    return EvidencePackage(
        format=EVIDENCE_FORMAT,
        waybill_id=waybill_id,
        origin_party=waybill["origin_party"],
        revisions=revisions,
        chain=chain,
        digest=evidence_digest(waybill_id, waybill["origin_party"], revisions),
    )


def import_evidence(
    conn: sqlite3.Connection, package: EvidencePackage
) -> EvidenceImportResponse:
    """导入证据包并原子合并到本库。

    三道完整性核对（任一失败 -> 422，本库不变）：
      1. 修订唯一性：包内 (scan_id, revision) 不得重复；
      2. 摘要：按包内规范化内容重算的 digest 必须与包声明的一致；
      3. 可信链：由包内修订重算的可信链必须与包内嵌入的一致。

    合并规则（单个 BEGIN IMMEDIATE 事务，违反即整包回滚）：
      * 目标库无此运单 -> 按包内起始保管方建档；
      * 已有同号运单但起始保管方不同 -> 409 整包拒绝；
      * 共同修订内容必须完全相同（同修订异文 -> 409 整包拒绝）；
      * 缺少的修订逐条补入；已存在且同文的修订幂等跳过，
        因此同一包重试无任何副作用。
    """
    revisions = package.revisions

    # 1. revision uniqueness inside the package
    seen: set[tuple[str, int]] = set()
    for entry in revisions:
        key = (entry.scan_id, entry.revision)
        if key in seen:
            raise PackageIntegrity(
                f"duplicate revision in package: scan_id={key[0]!r} revision={key[1]}"
            )
        seen.add(key)

    # 2. deterministic digest over the canonical package content
    recomputed = evidence_digest(package.waybill_id, package.origin_party, revisions)
    if recomputed != package.digest:
        raise PackageIntegrity(
            f"digest mismatch: package declares {package.digest!r} but the "
            f"recomputed digest is {recomputed!r}; package is corrupted"
        )

    # 3. the embedded chain must equal the chain recomputed from the package alone
    rows = [_canonical_row(e) for e in revisions]
    chain = compute_chain_from_rows(package.waybill_id, package.origin_party, rows)
    if chain != package.chain:
        raise PackageIntegrity(
            "embedded chain does not match the chain recomputed from the "
            "package revisions; package is corrupted"
        )

    imported = 0
    already_present = 0
    from .database import transaction

    with transaction(conn):
        row = conn.execute(
            "SELECT origin_party FROM waybills WHERE id = ?", (package.waybill_id,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO waybills (id, origin_party, created_at) VALUES (?, ?, ?)",
                (package.waybill_id, package.origin_party, utcnow_iso()),
            )
        elif row["origin_party"] != package.origin_party:
            raise DomainConflict(
                f"waybill {package.waybill_id!r} already exists with origin_party "
                f"{row['origin_party']!r}; package declares {package.origin_party!r} "
                "-> whole package rejected"
            )

        for entry in revisions:
            h = entry_payload_hash(entry)
            existing = conn.execute(
                "SELECT payload_hash FROM scans "
                "WHERE waybill_id = ? AND scan_id = ? AND revision = ?",
                (package.waybill_id, entry.scan_id, entry.revision),
            ).fetchone()
            if existing is not None:
                # 共同修订须完全相同；同修订异文 -> 整包拒绝并回滚
                if existing["payload_hash"] != h:
                    raise DomainConflict(
                        f"scan_id={entry.scan_id!r} revision={entry.revision} already "
                        "exists with different content (same-revision divergence) "
                        "-> whole package rejected"
                    )
                already_present += 1
                continue
            _insert_revision(conn, package.waybill_id, entry, h)
            imported += 1

    # Merged chain recomputed against the freshly committed snapshot.
    merged = compute_chain(conn, package.waybill_id)
    return EvidenceImportResponse(
        waybill_id=package.waybill_id,
        digest=package.digest,
        imported_revisions=imported,
        already_present=already_present,
        chain=merged,
    )


# Re-export so other modules can reuse the domain exceptions.
__all__ = [
    "DomainConflict",
    "DomainValidation",
    "PackageIntegrity",
    "apply_batch",
    "compute_chain",
    "compute_chain_from_rows",
    "create_waybill",
    "export_evidence",
    "get_waybill",
    "import_evidence",
]
