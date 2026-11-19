"""Pydantic request/response models and payload canonicalization."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #
class WaybillCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    waybill_id: str = Field(min_length=1, description="运单号，全局唯一")
    origin_party: str = Field(min_length=1, description="固定起始保管方")

    @field_validator("waybill_id", "origin_party")
    @classmethod
    def _strip_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v


class ScanEntry(BaseModel):
    """One scan inside a batch submission.

    ``event_type`` defaults to HANDOVER so the common case only carries
    scan_id / ts_utc / giver / receiver / revision.
    """

    model_config = ConfigDict(extra="forbid")

    scan_id: str = Field(min_length=1)
    revision: int = Field(gt=0, description="正整数修订号；同 ID 仅最高修订生效")
    event_type: Literal["HANDOVER", "WITHDRAWAL"] = "HANDOVER"
    ts_utc: datetime | None = Field(
        default=None, description="ISO-8601，必须带时区偏移（推荐 UTC，如 2026-11-18T08:30:00Z）"
    )
    giver: str | None = None
    receiver: str | None = None

    @field_validator("scan_id")
    @classmethod
    def _strip_scan_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v

    @field_validator("giver", "receiver")
    @classmethod
    def _strip_parties(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @field_validator("ts_utc")
    @classmethod
    def _require_timezone(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("ts_utc must carry an explicit timezone offset, e.g. ...Z or +08:00")
        return v

    @model_validator(mode="after")
    def _check_shape(self) -> "ScanEntry":
        if self.event_type == "HANDOVER":
            missing = [
                name
                for name, val in (
                    ("ts_utc", self.ts_utc),
                    ("giver", self.giver),
                    ("receiver", self.receiver),
                )
                if val is None
            ]
            if missing:
                raise ValueError(f"HANDOVER entry requires fields: {', '.join(missing)}")
        else:  # WITHDRAWAL: only id + higher revision are accepted
            extras = [
                name
                for name, val in (
                    ("ts_utc", self.ts_utc),
                    ("giver", self.giver),
                    ("receiver", self.receiver),
                )
                if val is not None
            ]
            if extras:
                raise ValueError(
                    f"WITHDRAWAL entry must only carry scan_id and revision; got extra: {', '.join(extras)}"
                )
        return self


class BatchSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scans: list[ScanEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_batch_duplicates(self) -> "BatchSubmit":
        # Same revision appearing twice in one batch with different text is a
        # whole-batch rejection; identical text is treated as idempotent.
        seen: dict[tuple[str, int], str] = {}
        for entry in self.scans:
            key = (entry.scan_id, entry.revision)
            h = entry_payload_hash(entry)
            if key in seen and seen[key] != h:
                raise ValueError(
                    f"scan_id={key[0]} revision={key[1]} appears multiple times "
                    "with conflicting content in the same batch"
                )
            seen[key] = h
        return self


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #
class BreakReason(str, Enum):
    none = "NONE"
    gap = "GAP"  # 保管方不承接：giver != 前一 receiver
    simultaneous = "SIMULTANEOUS"  # 同一时刻多笔交接，顺序不可判定


class ScanView(BaseModel):
    scan_id: str
    revision: int
    event_type: Literal["HANDOVER", "WITHDRAWAL"]
    ts_utc: datetime | None
    giver: str | None
    receiver: str | None
    verified: bool
    unverified_reason: BreakReason | None = None


class ChainResponse(BaseModel):
    waybill_id: str
    origin_party: str
    current_custodian: str
    verified_prefix: list[ScanView]
    unverified_scans: list[ScanView]
    chain_intact: bool
    break_reason: BreakReason
    break_detail: str | None = None
    break_scan_ids: list[str] = Field(default_factory=list)


class BatchResponse(BaseModel):
    waybill_id: str
    applied: int
    idempotent_duplicates: int
    withdrawn_scan_ids: list[str]
    chain: ChainResponse


# --------------------------------------------------------------------------- #
# Evidence packages (offline-archive export / import)
# --------------------------------------------------------------------------- #
EVIDENCE_FORMAT = "custody-evidence/v1"


class EvidencePackage(BaseModel):
    """离线证据包：运单号、起始保管方、全部扫描修订（含撤回）、可信链与确定性摘要。

    ``digest`` 覆盖 ``format`` / ``waybill_id`` / ``origin_party`` 以及规范化后
    按 ``(scan_id, revision)`` 排序的全部修订；导入侧据此核对包完整性，
    并重算 ``revisions`` 对应的可信链与内嵌 ``chain`` 比对。
    """

    model_config = ConfigDict(extra="forbid")

    format: Literal["custody-evidence/v1"] = EVIDENCE_FORMAT
    waybill_id: str = Field(min_length=1)
    origin_party: str = Field(min_length=1)
    revisions: list[ScanEntry] = Field(
        default_factory=list,
        description="全部修订历史（含被更高修订覆盖的版本与撤回墓碑），不止当前生效版本",
    )
    chain: ChainResponse
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("waybill_id", "origin_party")
    @classmethod
    def _strip_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v


class EvidenceImportResponse(BaseModel):
    waybill_id: str
    digest: str
    imported_revisions: int = Field(description="本次新补入的修订条数")
    already_present: int = Field(description="本库已存在且内容完全一致的修订条数（幂等部分）")
    chain: ChainResponse = Field(description="合并完成后的可信链")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_utc_iso(dt: datetime) -> str:
    """Normalize any timezone-aware datetime to a fixed-width UTC ISO string."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def canonical_payload(entry: ScanEntry) -> dict:
    """Deterministic representation used to detect same-revision divergence."""
    if entry.event_type == "WITHDRAWAL":
        return {"event_type": "WITHDRAWAL"}
    return {
        "event_type": "HANDOVER",
        "ts_utc": to_utc_iso(entry.ts_utc),  # type: ignore[arg-type]
        "giver": entry.giver,
        "receiver": entry.receiver,
    }


def payload_hash_of(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def entry_payload_hash(entry: ScanEntry) -> str:
    return payload_hash_of(canonical_payload(entry))


def canonical_revision(entry: ScanEntry) -> dict:
    """Deterministic per-revision representation used by the evidence digest."""
    return {
        "scan_id": entry.scan_id,
        "revision": entry.revision,
        **canonical_payload(entry),
    }


def evidence_digest(
    waybill_id: str, origin_party: str, revisions: list[ScanEntry]
) -> str:
    """确定性摘要：规范化修订按 (scan_id, revision) 排序后的 SHA-256。

    同一逻辑内容在任何库中重算都得到同一摘要，与导出先后、字段顺序无关。
    """
    ordered = sorted(revisions, key=lambda e: (e.scan_id, e.revision))
    payload = {
        "format": EVIDENCE_FORMAT,
        "waybill_id": waybill_id,
        "origin_party": origin_party,
        "revisions": [canonical_revision(e) for e in ordered],
    }
    return "sha256:" + payload_hash_of(payload)
