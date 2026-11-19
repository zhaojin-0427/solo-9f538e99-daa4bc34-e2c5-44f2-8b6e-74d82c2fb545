"""FastAPI application: dangerous-goods custody chain API."""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError

from . import config, services
from .database import connect, init_db
from .schemas import (
    BatchResponse,
    BatchSubmit,
    ChainResponse,
    EvidenceImportResponse,
    EvidencePackage,
    WaybillCreate,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> None:
    init_db()
    yield


app = FastAPI(
    title="空运危险品保管链 API",
    description=(
        "运单保管链（扫描交接 / 撤回 / 补报 / 更正）的修订裁决与可信前缀查询。"
        "同 ID 仅最高修订生效；同修订异文整批拒绝；失败保留旧链。"
        "支持证据包导出 / 导入：把一张运单的全部扫描修订从离线库原子并入另一库。"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


def get_conn() -> sqlite3.Connection:  # type: ignore[return]
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Error mapping: domain errors never leak 500s, and failed batches have
# already been rolled back by the service layer.
# --------------------------------------------------------------------------- #
@app.exception_handler(services.DomainConflict)
async def conflict_handler(_: Request, exc: services.DomainConflict) -> JSONResponse:
    return JSONResponse(status_code=409, content={"error": "conflict", "detail": str(exc)})


@app.exception_handler(services.DomainValidation)
async def not_found_handler(_: Request, exc: services.DomainValidation) -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": "not_found", "detail": str(exc)})


@app.exception_handler(services.PackageIntegrity)
async def package_integrity_handler(
    _: Request, exc: services.PackageIntegrity
) -> JSONResponse:
    return JSONResponse(
        status_code=422, content={"error": "package_integrity", "detail": str(exc)}
    )


@app.exception_handler(ValidationError)
async def pydantic_handler(_: Request, exc: ValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"error": "validation_error", "detail": exc.errors()})


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.post("/api/waybills", status_code=201, tags=["waybills"])
def create_waybill(
    body: WaybillCreate, conn: sqlite3.Connection = Depends(get_conn)
) -> dict:
    """创建运单并固定其起始保管方。"""
    return services.create_waybill(conn, body)


@app.get("/api/waybills", tags=["waybills"])
def list_waybills(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    rows = conn.execute(
        "SELECT id, origin_party, created_at FROM waybills ORDER BY id"
    ).fetchall()
    return {"waybills": [dict(r) for r in rows]}


@app.post(
    "/api/waybills/{waybill_id}/scans/batch",
    response_model=BatchResponse,
    tags=["scans"],
)
def submit_batch(
    waybill_id: str,
    batch: BatchSubmit,
    conn: sqlite3.Connection = Depends(get_conn),
) -> BatchResponse:
    """批量提交交接/撤回。

    * HANDOVER：scan_id、revision、ts_utc（带时区）、giver、receiver
    * WITHDRAWAL：仅 scan_id、revision（必须高于已知最高修订）
    * 同修订异文、过期修订、撤回未知 ID -> 409，整批回滚，旧链保留
    """
    return services.apply_batch(conn, waybill_id, batch)


@app.get(
    "/api/waybills/{waybill_id}/chain",
    response_model=ChainResponse,
    tags=["scans"],
)
def query_chain(
    waybill_id: str, conn: sqlite3.Connection = Depends(get_conn)
) -> ChainResponse:
    """返回可信连续交接前缀、首处断链原因与当前可确认保管方。"""
    return services.compute_chain(conn, waybill_id)


# --------------------------------------------------------------------------- #
# Evidence packages: merge one waybill's scan evidence between archives
# --------------------------------------------------------------------------- #
@app.get(
    "/api/waybills/{waybill_id}/evidence",
    response_model=EvidencePackage,
    tags=["evidence"],
)
def export_evidence(
    waybill_id: str, conn: sqlite3.Connection = Depends(get_conn)
) -> EvidencePackage:
    """导出证据包（全部内容取自同一个已提交快照）。

    包含：运单号、起始保管方、全部扫描修订与撤回记录（含被更高修订
    覆盖的历史版本）、按当前规则重算的可信链，以及覆盖上述内容的
    确定性摘要 ``digest``。
    """
    return services.export_evidence(conn, waybill_id)


@app.post(
    "/api/evidence/import",
    response_model=EvidenceImportResponse,
    tags=["evidence"],
)
def import_evidence(
    package: EvidencePackage, conn: sqlite3.Connection = Depends(get_conn)
) -> EvidenceImportResponse:
    """导入证据包，把离线库中的扫描证据原子并入本库。

    * 核对：修订唯一性、确定性摘要、包内重算的可信链 —— 任一不符 -> 422，本库不变
    * 目标库已有同号运单：起始保管方不同或共同修订异文 -> 409 整包拒绝
    * 缺少的修订补入；已存在且同文的修订幂等跳过（同包重试无副作用）
    * 整个合并位于单个事务中：损坏包、并发导入或中途失败都不会留下部分修订
    * 成功返回合并后的可信链；导入后仍可按原规则补报、更正、撤回和查询
    """
    return services.import_evidence(conn, package)


# --------------------------------------------------------------------------- #
# Minimal console (the interactive /docs remains fully functional too).
# --------------------------------------------------------------------------- #
_STATIC_DIR = Path(__file__).parent / "static"


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")
