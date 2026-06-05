from __future__ import annotations

import asyncio
import json
import logging
import ntpath
import os
import posixpath
import re
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import store
from .aria2 import Aria2Client, Aria2Error
from .config import settings
from .schemas import (
    BatchCreate, GlobalStat, HePushRequest, HeSyncRequest, HeXImportRequest, JobCreate, JobFile, JobView,
)

log = logging.getLogger("gateway")

RECONCILE_INTERVAL = 1.0

# 完成回调（M5）：任务跑到这些终态时回调 callback_url。HE 据此触发 scanner 入库。
# 用户主动 cancel/delete 不回调（不经对账、状态直接置 canceled）。
CALLBACK_EVENTS = {"complete", "error"}
CALLBACK_RETRIES = 4          # 网络错误/5xx 时的重试次数
CALLBACK_BACKOFF = 2.0        # 指数退避基数（秒）

# 持有在飞的回调任务引用，否则可能被 GC 提前回收（asyncio 已知坑）。
_callback_tasks: set[asyncio.Task] = set()


# --- 生命周期：aria2 客户端 + DB + 对账协程 --------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    http = httpx.AsyncClient(timeout=30.0)
    app.state.http = http
    app.state.aria2 = Aria2Client(settings.aria2_rpc_url, settings.aria2_rpc_secret, http)
    store.init()
    task = asyncio.create_task(_reconcile_loop(app.state.aria2))
    app.state.reconcile_task = task
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await http.aclose()


app = FastAPI(title="HE_downloader gateway", version="0.3.0", lifespan=lifespan)

# Web 面板（静态文件由 gateway 顺带托管，无需 node 构建 / 额外容器）。
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


def get_aria2() -> Aria2Client:
    return app.state.aria2


# --- 鉴权（token 为空则放行） -----------------------------------------------
# 同时接受 Authorization: Bearer 头 与 ?token= 查询参数 —— 后者给浏览器
# EventSource 用（SSE 客户端无法自定义请求头）。面板页 `/` 与 /static 不鉴权，
# 否则连加载页面输入 token 都做不到。

async def require_auth(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = None,
) -> None:
    secret = settings.gateway_api_token
    if not secret:
        return
    if authorization == f"Bearer {secret}" or token == secret:
        return
    raise HTTPException(status_code=401, detail="缺少或无效的 token")


# --- aria2 选项 / 状态映射 ---------------------------------------------------

def _resolve_dir(dest_dir: Optional[str]) -> str:
    base = settings.download_dir
    if not dest_dir:
        return base
    if _is_absolute_download_path(dest_dir):
        return dest_dir
    return _join_download_path(base, dest_dir)


def _is_windows_path(path: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", path or "") or (path or "").startswith("\\\\"))


def _is_absolute_download_path(path: str) -> bool:
    return bool(posixpath.isabs(path or "") or _is_windows_path(path or ""))


def _join_download_path(base: str, *parts: str) -> str:
    if _is_windows_path(base):
        return ntpath.join(base, *[p.replace("/", "\\") for p in parts if p])
    return posixpath.join(base, *[p.replace("\\", "/") for p in parts if p])


def _normalize_proxy(value: Optional[str]) -> str:
    raw = (value or "").strip()
    if raw.lower() in {"none", "direct", "off", "0"}:
        return ""
    return raw


def _download_proxy() -> str:
    return _normalize_proxy(store.get_setting("aria2_all_proxy", settings.aria2_all_proxy))


def _apply_download_proxy(options: dict) -> dict:
    proxy = _download_proxy()
    if proxy:
        options["all-proxy"] = proxy
    return options


def _build_options(type: str, resolved_dir: str, filename: Optional[str], headers: Optional[dict]) -> dict:
    options: dict[str, object] = {"dir": resolved_dir}
    if filename:
        options["out"] = filename
    if headers:
        options["header"] = [f"{k}: {v}" for k, v in headers.items()]
    if type == "magnet":
        options["seed-time"] = "0"      # 下完即停、不做种
        options["bt-stop-timeout"] = "120"
        options["bt-save-metadata"] = "true"
        options["bt-load-saved-metadata"] = "true"
        trackers = (settings.bt_trackers or "").strip().strip(",")
        if trackers:
            options["bt-tracker"] = trackers
    return _apply_download_proxy(options)


def _build_file_options(dest_dir: str, rel_path: str, headers: Optional[dict]) -> dict:
    """分组任务里单个文件的 aria2 选项：dir = dest_dir + rel 的目录段，out = 文件名。"""
    if _is_windows_path(dest_dir):
        normalized_rel = (rel_path or "").replace("/", "\\")
        sub = ntpath.dirname(normalized_rel)
        out = ntpath.basename(normalized_rel)
    else:
        normalized_rel = (rel_path or "").replace("\\", "/")
        sub = posixpath.dirname(normalized_rel)
        out = posixpath.basename(normalized_rel)
    options: dict[str, object] = {"dir": _join_download_path(dest_dir, sub) if sub else dest_dir}
    if out:
        options["out"] = out
    if headers:
        options["header"] = [f"{k}: {v}" for k, v in headers.items()]
    return _apply_download_proxy(options)


_STATUS_MAP = {"complete": "complete", "error": "error", "removed": "canceled"}


def _aria_error_message(raw: dict) -> Optional[str]:
    message = (raw.get("errorMessage") or "").strip()
    if message:
        return message
    code = str(raw.get("errorCode") or "").strip()
    if not code:
        return None
    if code == "7":
        if str(raw.get("totalLength") or "0") == "0":
            return "未获取到磁力元数据：当前没有可用 peer/seed，或 tracker/DHT 网络不可达"
        return "BT 下载中断：当前没有足够可用 peer/seed，可稍后重试续传"
    return f"aria2 errorCode={code}"


def _aggregate_batch(files: list[dict]) -> dict:
    """把子文件状态聚合成父 job 的 status/进度/速度。"""
    total = sum(int(f.get("total_bytes") or 0) for f in files)
    completed = sum(int(f.get("completed_bytes") or 0) for f in files)
    speed = sum(int(f.get("download_speed") or 0) for f in files if f.get("status") == "active")
    required = [f for f in files if not f.get("optional")]
    optional = [f for f in files if f.get("optional")]
    required_sts = [f.get("status") for f in required]
    all_sts = [f.get("status") for f in files]
    optional_terminal = all((f.get("status") in ("complete", "error", "canceled")) for f in optional)
    if files and required and all(s == "complete" for s in required_sts) and optional_terminal:
        status = "complete"
    elif any(s in ("active", "waiting", "pending") for s in all_sts):
        status = "active"
    elif any(s == "paused" for s in all_sts):
        status = "paused"
    elif any(s == "error" for s in required_sts):
        status = "error"
    else:
        status = "canceled"
    n_err = sum(1 for s in required_sts if s == "error")
    return {
        "status": status,
        "total_bytes": total,
        "completed_bytes": completed,
        "download_speed": speed,
        "error": f"{n_err} 个文件失败" if n_err else None,
    }


def _map_raw(raw: dict) -> dict:
    """aria2 tellStatus 结果 → store.update_progress 的字段。"""
    total = int(raw.get("totalLength", 0) or 0)
    completed = int(raw.get("completedLength", 0) or 0)
    aria_status = raw.get("status", "")
    status = _STATUS_MAP.get(aria_status, aria_status)

    name = ""
    bt = raw.get("bittorrent") or {}
    if isinstance(bt, dict):
        name = (bt.get("info") or {}).get("name", "") or ""
    files_raw = raw.get("files", []) or []
    if not name and files_raw:
        first_path = files_raw[0].get("path", "")
        name = os.path.basename(first_path) if first_path else ""

    return {
        "status": status,
        "name": name,
        "total_bytes": total,
        "completed_bytes": completed,
        "download_speed": int(raw.get("downloadSpeed", 0) or 0),
        "error": _aria_error_message(raw) if status == "error" else None,
    }


def _batch_file_views(job_id: str) -> list[JobFile]:
    """分组任务的子文件列表（供面板展开逐文件看进度）。"""
    out: list[JobFile] = []
    for f in store.batch_files(job_id):
        total = int(f.get("total_bytes") or 0)
        completed = int(f.get("completed_bytes") or 0)
        rel = f.get("rel_path") or ""
        out.append(JobFile(
            name=posixpath.basename(rel) or rel or f.get("url") or "",
            rel_path=rel,
            optional=bool(f.get("optional")),
            status=f.get("status") or "pending",
            total_bytes=total,
            completed_bytes=completed,
            download_speed=int(f.get("download_speed") or 0),
            progress=round(completed / total * 100, 1) if total else 0.0,
            error=f.get("error"),
        ))
    return out


def _row_to_view(row: dict) -> JobView:
    total = int(row.get("total_bytes", 0) or 0)
    completed = int(row.get("completed_bytes", 0) or 0)
    return JobView(
        id=row["id"],
        type=row["type"],
        status=row["status"],
        name=row.get("name") or "",
        total_bytes=total,
        completed_bytes=completed,
        download_speed=int(row.get("download_speed", 0) or 0),
        progress=round(completed / total * 100, 1) if total else 0.0,
        dir=row.get("dest_dir") or settings.download_dir,
        error=row.get("error"),
        created_at=float(row.get("created_at", 0) or 0),
        files=_batch_file_views(row["id"]) if row.get("type") == "batch" else [],
    )


# --- 完成回调 webhook（M5） -------------------------------------------------

async def _deliver_callback(http: httpx.AsyncClient, job_id: str, url: str, payload: dict) -> None:
    """带重试地 POST 回调。4xx 视为送达（接收方收到了但拒绝，重试无意义）；
    网络错误 / 5xx 才退避重试。失败到底只记日志（callback_fired 已置 1，不再补发）。"""
    for attempt in range(CALLBACK_RETRIES):
        try:
            resp = await http.post(url, json=payload, timeout=30.0)
            if resp.status_code < 500:
                log.info("callback %s -> %s (%s)", job_id, url, resp.status_code)
                return
            log.warning("callback %s got %s, retrying", job_id, resp.status_code)
        except httpx.HTTPError as exc:
            log.warning("callback %s attempt %d failed: %s", job_id, attempt + 1, exc)
        await asyncio.sleep(CALLBACK_BACKOFF * (2 ** attempt))
    log.error("callback %s gave up after %d attempts: %s", job_id, CALLBACK_RETRIES, url)


def _maybe_fire_callback(http: httpx.AsyncClient, job_id: str) -> None:
    """若 job 已到终态且配了 callback_url 且尚未派发，则认领并后台投递回调。"""
    row = store.get(job_id)
    if not row or not row.get("callback_url"):
        return
    if row.get("status") not in CALLBACK_EVENTS:
        return
    if not store.claim_callback(job_id):   # 已派发过 / 被别的对账拍抢走
        return
    view = _row_to_view(row)
    payload = {"event": row["status"], "job": view.model_dump()}
    task = asyncio.create_task(_deliver_callback(http, job_id, row["callback_url"], payload))
    _callback_tasks.add(task)
    task.add_done_callback(_callback_tasks.discard)


# --- 对账协程：DB 为真相源，aria2 重启则据 DB 重新入队 ----------------------

async def _reconcile_once(aria2: Aria2Client) -> None:
    active = await aria2.tell_active()
    waiting = await aria2.tell_waiting()
    stopped = await aria2.tell_stopped()
    by_gid = {r.get("gid"): r for r in [*active, *waiting, *stopped] if r.get("gid")}

    for job in store.non_terminal():
        if job.get("type") == "batch":
            await _reconcile_batch(job, by_gid, aria2)
            continue
        gid = job.get("gid")
        raw = by_gid.get(gid) if gid else None
        if raw is not None:
            followed_by = raw.get("followedBy") or []
            if followed_by:
                next_gid = followed_by[0]
                store.set_gid(job["id"], next_gid)
                next_raw = by_gid.get(next_gid)
                if next_raw is not None:
                    store.update_progress(job["id"], **_map_raw(next_raw))
                else:
                    store.set_status(job["id"], "active")
                continue
            store.update_progress(job["id"], **_map_raw(raw))
            _maybe_fire_callback(app.state.http, job["id"])
            continue
        # aria2 不认识这个 gid（刚崩溃重启丢了队列，或 job 还没拿到 gid）→ 据 DB 重新入队。
        # 配合下载目录里的 .aria2 控制文件 + aria2 --continue，已下的部分会续传。
        opts = _build_options(job["type"], job.get("dest_dir") or settings.download_dir,
                              job.get("filename"), store.headers_of(job))
        try:
            new_gid = await aria2.add_uri([job["source"]], opts)
            store.set_gid(job["id"], new_gid)
            store.set_status(job["id"], "active")
            log.info("requeued job %s -> gid %s", job["id"], new_gid)
        except Aria2Error as exc:
            store.set_status(job["id"], "error", str(exc))


async def _reconcile_batch(job: dict, by_gid: dict, aria2: Aria2Client) -> None:
    """分组任务：逐子文件同步/补入队，再把聚合结果写回父 job 行。"""
    dest = job.get("dest_dir") or settings.download_dir
    for f in store.batch_files(job["id"]):
        if f.get("status") in store.TERMINAL:
            continue
        gid = f.get("gid")
        raw = by_gid.get(gid) if gid else None
        if raw is not None:
            m = _map_raw(raw)
            store.file_update(f["id"], status=m["status"], total_bytes=m["total_bytes"],
                              completed_bytes=m["completed_bytes"],
                              download_speed=int(raw.get("downloadSpeed", 0) or 0),
                              error=m["error"])
        elif gid is None:
            opts = _build_file_options(dest, f.get("rel_path") or "", store.headers_of(f))
            try:
                ngid = await aria2.add_uri([f["url"]], opts)
                store.file_set_gid(f["id"], ngid)
                store.file_update(f["id"], status="active", total_bytes=int(f.get("total_bytes") or 0),
                                  completed_bytes=int(f.get("completed_bytes") or 0))
            except Aria2Error as exc:
                store.file_update(f["id"], status="error", total_bytes=0, completed_bytes=0, error=str(exc))
        else:
            # gid 在 aria2 里消失了（重启丢队列）→ 清掉 gid，下一拍重新入队续传。
            store.file_set_gid(f["id"], None)

    files = store.batch_files(job["id"])
    agg = _aggregate_batch(files)
    store.update_progress(job["id"], name=job.get("name") or "", **agg)
    _maybe_fire_callback(app.state.http, job["id"])


async def _reconcile_loop(aria2: Aria2Client) -> None:
    while True:
        try:
            await _reconcile_once(aria2)
        except asyncio.CancelledError:
            raise
        except (Aria2Error, httpx.HTTPError) as exc:
            log.debug("reconcile skipped: %s", exc)  # aria2 还没起来等下一拍
        except Exception:  # noqa: BLE001
            log.exception("reconcile error")
        await asyncio.sleep(RECONCILE_INTERVAL)


# --- 路由 -------------------------------------------------------------------

@app.get("/health")
async def health(aria2: Aria2Client = Depends(get_aria2)):
    try:
        version = await aria2.get_version()
        return {"status": "ok", "aria2": version.get("version")}
    except (Aria2Error, httpx.HTTPError) as exc:
        raise HTTPException(status_code=503, detail=f"aria2 不可达: {exc}")


@app.get("/settings", dependencies=[Depends(require_auth)])
async def get_settings():
    return {"aria2_all_proxy": _download_proxy()}


@app.put("/settings", dependencies=[Depends(require_auth)])
async def update_settings(payload: dict):
    if "aria2_all_proxy" in payload:
        store.set_setting("aria2_all_proxy", _normalize_proxy(payload.get("aria2_all_proxy")))
    return {"aria2_all_proxy": _download_proxy()}


@app.post("/jobs", response_model=JobView, dependencies=[Depends(require_auth)])
async def create_job(payload: JobCreate, aria2: Aria2Client = Depends(get_aria2)):
    resolved = _resolve_dir(payload.dest_dir)
    job_id = store.create(
        type=payload.type, source=payload.source, dest_dir=resolved,
        filename=payload.filename, headers=payload.headers, callback_url=payload.callback_url,
    )
    opts = _build_options(payload.type, resolved, payload.filename, payload.headers)
    try:
        gid = await aria2.add_uri([payload.source], opts)
    except Aria2Error as exc:
        store.set_status(job_id, "error", str(exc))
        raise HTTPException(status_code=400, detail=str(exc))
    except httpx.HTTPError as exc:
        store.set_status(job_id, "error", f"aria2 不可达: {exc}")
        raise HTTPException(status_code=503, detail=f"aria2 不可达: {exc}")
    store.set_gid(job_id, gid)
    store.set_status(job_id, "active")
    return _row_to_view(store.get(job_id))


@app.post("/jobs/batch", response_model=JobView, dependencies=[Depends(require_auth)])
async def create_batch_job(payload: BatchCreate, aria2: Aria2Client = Depends(get_aria2)):
    """一次提交多文件作为一个分组任务（如一本漫画/一个 ASMR）。HE Manager 走这里推收藏。"""
    if not payload.files:
        raise HTTPException(status_code=400, detail="files 不能为空")
    resolved = _resolve_dir(payload.dest_dir)
    job_id = store.create_batch(
        name=payload.name, dest_dir=resolved, callback_url=payload.callback_url,
        files=[
            {"url": f.url, "rel_path": f.rel_path, "headers": f.headers, "optional": f.optional}
            for f in payload.files
        ],
    )
    # 立即入队（不等对账那一拍），失败的文件标 error，由对账兜底/重试。
    for f in store.batch_files(job_id):
        opts = _build_file_options(resolved, f.get("rel_path") or "", store.headers_of(f))
        try:
            gid = await aria2.add_uri([f["url"]], opts)
            store.file_set_gid(f["id"], gid)
            store.file_update(f["id"], status="active", total_bytes=0, completed_bytes=0)
        except (Aria2Error, httpx.HTTPError) as exc:
            store.file_update(f["id"], status="error", total_bytes=0, completed_bytes=0, error=str(exc))
    store.update_progress(job_id, name=payload.name, **_aggregate_batch(store.batch_files(job_id)))
    _maybe_fire_callback(app.state.http, job_id)  # 全失败即终态，不会再进对账，这里兜底回调
    return _row_to_view(store.get(job_id))


@app.get("/jobs", response_model=list[JobView], dependencies=[Depends(require_auth)])
async def list_jobs():
    return [_row_to_view(r) for r in store.list_all()]


@app.get("/jobs/{job_id}", response_model=JobView, dependencies=[Depends(require_auth)])
async def get_job(job_id: str):
    row = store.get(job_id)
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _row_to_view(row)


def _require_job(job_id: str) -> dict:
    row = store.get(job_id)
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    return row


async def _control(job: dict, aria2: Aria2Client, *, method: str, job_status: str, file_status: str) -> None:
    """对单任务的 gid 或分组任务所有子文件 gid 执行 aria2 操作 + 落库状态。"""
    if job.get("type") == "batch":
        for f in store.batch_files(job["id"]):
            if f.get("status") in store.TERMINAL:
                continue
            if f.get("gid"):
                try:
                    await getattr(aria2, method)(f["gid"])
                except Aria2Error:
                    pass
            store.file_set_status(f["id"], file_status)
    elif job.get("gid"):
        try:
            await getattr(aria2, method)(job["gid"])
        except Aria2Error:
            pass
    store.set_status(job["id"], job_status)


@app.post("/jobs/{job_id}/pause", response_model=JobView, dependencies=[Depends(require_auth)])
async def pause_job(job_id: str, aria2: Aria2Client = Depends(get_aria2)):
    await _control(_require_job(job_id), aria2, method="pause", job_status="paused", file_status="paused")
    return _row_to_view(store.get(job_id))


@app.post("/jobs/{job_id}/resume", response_model=JobView, dependencies=[Depends(require_auth)])
async def resume_job(job_id: str, aria2: Aria2Client = Depends(get_aria2)):
    await _control(_require_job(job_id), aria2, method="unpause", job_status="active", file_status="active")
    return _row_to_view(store.get(job_id))


@app.post("/jobs/{job_id}/cancel", response_model=JobView, dependencies=[Depends(require_auth)])
async def cancel_job(job_id: str, aria2: Aria2Client = Depends(get_aria2)):
    await _control(_require_job(job_id), aria2, method="remove", job_status="canceled", file_status="canceled")
    return _row_to_view(store.get(job_id))


@app.post("/jobs/{job_id}/retry", response_model=JobView, dependencies=[Depends(require_auth)])
async def retry_job(job_id: str, aria2: Aria2Client = Depends(get_aria2)):
    row = _require_job(job_id)
    if row["status"] not in store.TERMINAL:
        raise HTTPException(status_code=409, detail="任务未结束，无需重试")
    if row.get("type") == "batch":
        # 只重试失败/取消的子文件：清 gid + 置 pending，对账协程会重新入队。
        for f in store.batch_files(row["id"]):
            if f.get("status") in ("error", "canceled"):
                store.file_set_gid(f["id"], None)
                store.file_set_status(f["id"], "pending")
        store.set_status(job_id, "active")
        return _row_to_view(store.get(job_id))
    opts = _build_options(row["type"], row.get("dest_dir") or settings.download_dir,
                          row.get("filename"), store.headers_of(row))
    try:
        gid = await aria2.add_uri([row["source"]], opts)
    except Aria2Error as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    store.set_gid(job_id, gid)
    store.set_status(job_id, "active")
    return _row_to_view(store.get(job_id))


@app.delete("/jobs/{job_id}", dependencies=[Depends(require_auth)])
async def delete_job(job_id: str, aria2: Aria2Client = Depends(get_aria2)):
    row = _require_job(job_id)
    if row["status"] not in store.TERMINAL:
        await _control(row, aria2, method="remove", job_status="canceled", file_status="canceled")
    store.delete(job_id)
    return {"deleted": job_id}


@app.get("/events", dependencies=[Depends(require_auth)])
async def events():
    """SSE：任务列表有变化时推送一次完整快照（个人规模够用）。"""
    async def gen():
        last = None
        while True:
            payload = json.dumps(
                [_row_to_view(r).model_dump() for r in store.list_all()],
                ensure_ascii=False,
            )
            if payload != last:
                yield f"data: {payload}\n\n"
                last = payload
            await asyncio.sleep(RECONCILE_INTERVAL)

    return StreamingResponse(gen(), media_type="text/event-stream")


# --- 从 HE 收藏导入（网关代理 HE Manager） ----------------------------------
# 一站式：下载中心列出 HE 的 asmr/wnacg 收藏、勾选 → 触发 HE 解析并推回网关。
# HE 的地址 + token 留在网关服务端，前端只跟网关同源打交道（无 CORS、token 不进浏览器）。

def _he_configured() -> bool:
    return bool(settings.he_manager_url)


def _he_url(path: str) -> str:
    return settings.he_manager_url.rstrip("/") + path


def _he_headers() -> dict:
    return {"Authorization": f"Bearer {settings.he_manager_token}"} if settings.he_manager_token else {}


async def _he_get(path: str, params: Optional[dict] = None):
    resp = await app.state.http.get(_he_url(path), params=params, headers=_he_headers(), timeout=30.0)
    resp.raise_for_status()
    return resp.json()


def _he_detail(resp) -> str:
    try:
        detail = resp.json().get("detail", "")
        return str(detail) if detail else ""
    except Exception:  # noqa: BLE001
        return ""


async def _he_request_json(method: str, path: str, *, json_body: Optional[dict] = None, timeout: float = 30.0):
    if not _he_configured():
        raise HTTPException(status_code=503, detail="未配置 HE Manager（HE_MANAGER_URL）")
    try:
        resp = await app.state.http.request(
            method,
            _he_url(path),
            json=json_body,
            headers=_he_headers(),
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"HE 不可达: {exc}")
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=_he_detail(resp) or "HE 请求失败")
    return resp.json()


async def _he_x_source() -> dict:
    sources = await _he_request_json("GET", "/x/sources", timeout=30.0)
    if not sources:
        raise HTTPException(status_code=400, detail="HE 里还没有 X 来源")
    return sources[0]


@app.get("/he/enabled")
async def he_enabled():
    return {"enabled": _he_configured()}


@app.get("/he/sources", dependencies=[Depends(require_auth)])
async def he_sources():
    if not _he_configured():
        raise HTTPException(status_code=503, detail="未配置 HE Manager（HE_MANAGER_URL）")
    try:
        return await _he_get("/external/sources")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"HE 不可达: {exc}")


@app.get("/he/favorites", dependencies=[Depends(require_auth)])
async def he_favorites(source_type: Optional[str] = None, search: Optional[str] = None):
    if not _he_configured():
        raise HTTPException(status_code=503, detail="未配置 HE Manager（HE_MANAGER_URL）")
    params = {}
    if source_type:
        params["source_type"] = source_type
    if search:
        params["search"] = search
    try:
        return await _he_get("/external/favorites", params or None)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"HE 不可达: {exc}")


@app.post("/he/push", dependencies=[Depends(require_auth)])
async def he_push(payload: HePushRequest):
    if not _he_configured():
        raise HTTPException(status_code=503, detail="未配置 HE Manager（HE_MANAGER_URL）")
    # 该来源的下载位置（HE 的 push 端点要求传 download_root_path）从 source 取。
    try:
        sources = await _he_get("/external/sources")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"HE 不可达: {exc}")
    root = ""
    for s in sources:
        if (s.get("source_type") or "wnacg") == payload.source_type:
            root = (s.get("download_root_path") or "").strip()
            break
    if not root:
        raise HTTPException(status_code=400, detail=f"HE 里 {payload.source_type} 来源没设下载位置")
    try:
        resp = await app.state.http.post(
            _he_url(f"/external/{payload.source_type}/push"),
            json={"item_ids": payload.item_ids, "download_root_path": root},
            headers=_he_headers(), timeout=120.0,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"HE 不可达: {exc}")
    if resp.status_code != 200:
        detail = ""
        try:
            detail = resp.json().get("detail", "")
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(status_code=resp.status_code, detail=str(detail) or "HE 推送失败")
    return resp.json()


@app.post("/he/sync", dependencies=[Depends(require_auth)])
async def he_sync(payload: HeSyncRequest):
    """重新同步某来源的收藏：HE 据已存的 cookie/token 去源站重拉，刷新它的收藏 DB。
    凭据留在 HE 服务端（只传 source_id），不进浏览器。首次配置仍在 HE 自己的面板。"""
    if not _he_configured():
        raise HTTPException(status_code=503, detail="未配置 HE Manager（HE_MANAGER_URL）")
    try:
        sources = await _he_get("/external/sources")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"HE 不可达: {exc}")
    src = next((s for s in sources if (s.get("source_type") or "wnacg") == payload.source_type), None)
    if not src:
        raise HTTPException(
            status_code=400,
            detail=f"HE 里还没配置 {payload.source_type} 来源，请先去 HE Manager 同步一次",
        )
    fav_url = (src.get("favorites_url") or "").strip()
    if not fav_url:
        raise HTTPException(status_code=400, detail=f"{payload.source_type} 来源缺少地址，请先去 HE 配置")
    # 复用 HE 已存的凭据：只带 source_id（+ 地址），不传 cookie/账号密码。
    if payload.source_type == "asmr":
        body = {"source_id": src["id"], "api_base": fav_url}
    else:
        body = {"source_id": src["id"], "favorites_url": fav_url}
    try:
        resp = await app.state.http.post(
            _he_url(f"/external/{payload.source_type}/sync"),
            json=body, headers=_he_headers(), timeout=180.0,  # sync 逐页抓源站，给足时间
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"HE 不可达: {exc}")
    if resp.status_code != 200:
        detail = ""
        try:
            detail = resp.json().get("detail", "")
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(status_code=resp.status_code, detail=str(detail) or "HE 同步失败")
    data = resp.json()
    return {"synced_count": data.get("synced_count", 0), "source_type": payload.source_type}


@app.get("/he/x/state", dependencies=[Depends(require_auth)])
async def he_x_state():
    source = await _he_x_source()
    source_id = source["id"]
    state = {"source": source, "stats": None, "active_job": None, "active_sync": None}
    try:
        state["stats"] = await _he_request_json("GET", f"/x/sources/{source_id}/stats", timeout=30.0)
    except HTTPException as exc:
        state["stats_error"] = exc.detail
    try:
        state["active_job"] = await _he_request_json("GET", f"/x/sources/{source_id}/active-job", timeout=30.0)
    except HTTPException as exc:
        state["active_job_error"] = exc.detail
    try:
        state["active_sync"] = await _he_request_json("GET", f"/x/sources/{source_id}/active-sync", timeout=30.0)
    except HTTPException as exc:
        state["active_sync_error"] = exc.detail
    return state


@app.post("/he/x/sync", dependencies=[Depends(require_auth)])
async def he_x_sync():
    source = await _he_x_source()
    return await _he_request_json("POST", f"/x/sources/{source['id']}/sync", timeout=30.0)


@app.post("/he/x/imports", dependencies=[Depends(require_auth)])
async def he_x_import(payload: HeXImportRequest):
    source = await _he_x_source()
    body = {
        "source_id": source["id"],
        "retry_failed_only": payload.mode == "failed",
        "retry_skipped_only": payload.mode == "skipped",
    }
    return await _he_request_json("POST", "/x/imports", json_body=body, timeout=30.0)


@app.get("/he/x/imports/{job_id}", dependencies=[Depends(require_auth)])
async def he_x_import_job(job_id: str):
    return await _he_request_json("GET", f"/x/imports/{job_id}", timeout=30.0)


@app.post("/he/x/imports/{job_id}/{action}", dependencies=[Depends(require_auth)])
async def he_x_import_action(job_id: str, action: str):
    if action not in {"pause", "resume", "cancel"}:
        raise HTTPException(status_code=404, detail="未知 X 导入操作")
    return await _he_request_json("POST", f"/x/imports/{job_id}/{action}", timeout=30.0)


@app.get("/he/x/syncs/{job_id}", dependencies=[Depends(require_auth)])
async def he_x_sync_job(job_id: str):
    return await _he_request_json("GET", f"/x/syncs/{job_id}", timeout=30.0)


@app.post("/he/x/syncs/{job_id}/cancel", dependencies=[Depends(require_auth)])
async def he_x_sync_cancel(job_id: str):
    return await _he_request_json("POST", f"/x/syncs/{job_id}/cancel", timeout=30.0)


@app.get("/stat", response_model=GlobalStat, dependencies=[Depends(require_auth)])
async def global_stat(aria2: Aria2Client = Depends(get_aria2)):
    try:
        s = await aria2.get_global_stat()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"aria2 不可达: {exc}")
    return GlobalStat(
        download_speed=int(s.get("downloadSpeed", 0) or 0),
        upload_speed=int(s.get("uploadSpeed", 0) or 0),
        num_active=int(s.get("numActive", 0) or 0),
        num_waiting=int(s.get("numWaiting", 0) or 0),
        num_stopped=int(s.get("numStopped", 0) or 0),
    )
