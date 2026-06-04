"""M2 冒烟测试：DB 真相源 + 对账重入队 + 生命周期，全程用假 aria2，不需要真引擎。"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

# 必须在 import app 之前指定独立临时 DB（store 在 import 时读取 GATEWAY_DB_PATH）。
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["GATEWAY_DB_PATH"] = _tmp.name

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app import main, store  # noqa: E402
from app.main import app, get_aria2  # noqa: E402


class FakeAria2:
    def __init__(self):
        self.added: list[tuple[list[str], dict]] = []
        self.controls: list[tuple[str, str]] = []
        self.present = True   # aria2 是否还"认识"已加入的任务（模拟重启丢队列）
        self.done = False     # True = 所有任务下完（进 stopped，complete）
        self.gid_seq = 0
        self.gids: list[str] = []

    async def add_uri(self, uris, options=None):
        self.added.append((uris, options or {}))
        self.gid_seq += 1
        gid = f"gid-{self.gid_seq:03d}"
        self.gids.append(gid)
        return gid

    def _status(self, gid):
        if self.done:
            return {
                "gid": gid, "status": "complete",
                "totalLength": "1000", "completedLength": "1000", "downloadSpeed": "0",
                "dir": "/downloads",
                "files": [{"path": "/downloads/100MB.bin", "length": "1000", "completedLength": "1000"}],
            }
        return {
            "gid": gid, "status": "active",
            "totalLength": "1000", "completedLength": "250", "downloadSpeed": "500",
            "dir": "/downloads",
            "files": [{"path": "/downloads/100MB.bin", "length": "1000", "completedLength": "250"}],
        }

    async def tell_active(self):
        if not self.present or self.done:
            return []
        return [self._status(g) for g in self.gids]

    async def tell_waiting(self, *a):
        return []

    async def tell_stopped(self, *a):
        return [self._status(g) for g in self.gids] if (self.present and self.done) else []

    async def pause(self, gid):
        self.controls.append(("pause", gid)); return gid

    async def unpause(self, gid):
        self.controls.append(("unpause", gid)); return gid

    async def remove(self, gid):
        self.controls.append(("remove", gid)); return gid

    async def get_global_stat(self):
        return {"downloadSpeed": "500", "uploadSpeed": "0", "numActive": "1", "numWaiting": "0", "numStopped": "0"}

    async def get_version(self):
        return {"version": "1.37.0"}


@pytest.fixture
def client():
    f = FakeAria2()
    app.dependency_overrides[get_aria2] = lambda: f
    with TestClient(app) as c:
        store._conn.execute("DELETE FROM jobs")
        store._conn.commit()
        yield c, f
    app.dependency_overrides.clear()


def test_create_returns_active_job_with_stable_id(client):
    c, f = client
    r = c.post("/jobs", json={"type": "url", "source": "http://x/a.bin"})
    assert r.status_code == 200
    b = r.json()
    assert b["status"] == "active"
    assert b["id"] and len(b["id"]) >= 8           # uuid，不是 gid
    uris, opts = f.added[0]
    assert uris == ["http://x/a.bin"]
    assert opts["dir"].endswith("/downloads") or os.path.isabs(opts["dir"])
    assert "seed-time" not in opts
    # GET 回来还是同一个 id
    assert c.get(f"/jobs/{b['id']}").json()["id"] == b["id"]


def test_magnet_sets_no_seeding(client):
    c, f = client
    c.post("/jobs", json={"type": "magnet", "source": "magnet:?xt=urn:btih:abc"})
    _, opts = f.added[0]
    assert opts["seed-time"] == "0"


def test_headers_and_dest_dir(client):
    c, f = client
    c.post("/jobs", json={
        "type": "url", "source": "http://x/a.jpg",
        "dest_dir": "manga/book1", "filename": "001.jpg",
        "headers": {"Referer": "http://x"},
    })
    _, opts = f.added[0]
    assert opts["dir"].endswith("manga/book1")
    assert opts["out"] == "001.jpg"
    assert "Referer: http://x" in opts["header"]


def test_reconcile_updates_progress(client):
    c, f = client
    b = c.post("/jobs", json={"type": "url", "source": "http://x/a.bin"}).json()
    asyncio.run(main._reconcile_once(f))
    g = c.get(f"/jobs/{b['id']}").json()
    assert g["progress"] == 25.0
    assert g["name"] == "100MB.bin"
    assert g["total_bytes"] == 1000


def test_reconcile_requeues_when_aria2_forgot(client):
    c, f = client
    b = c.post("/jobs", json={"type": "url", "source": "http://x/a.bin"}).json()
    assert len(f.added) == 1
    # 模拟 aria2 重启：不再认识已加入的任务
    f.present = False
    asyncio.run(main._reconcile_once(f))
    # 对账应据 DB 重新入队
    assert len(f.added) == 2
    assert f.added[1][0] == ["http://x/a.bin"]
    assert c.get(f"/jobs/{b['id']}").json()["status"] == "active"


def test_lifecycle_pause_cancel(client):
    c, f = client
    b = c.post("/jobs", json={"type": "url", "source": "http://x/a.bin"}).json()
    gid = f"gid-{f.gid_seq:03d}"
    assert c.post(f"/jobs/{b['id']}/pause").json()["status"] == "paused"
    assert ("pause", gid) in f.controls
    assert c.post(f"/jobs/{b['id']}/cancel").json()["status"] == "canceled"
    assert ("remove", gid) in f.controls


def test_retry_requires_terminal_then_requeues(client):
    c, f = client
    b = c.post("/jobs", json={"type": "url", "source": "http://x/a.bin"}).json()
    # 还在跑时 retry 应 409
    assert c.post(f"/jobs/{b['id']}/retry").status_code == 409
    c.post(f"/jobs/{b['id']}/cancel")
    r = c.post(f"/jobs/{b['id']}/retry")
    assert r.status_code == 200
    assert r.json()["status"] == "active"
    assert len(f.added) == 2  # 原始 + 重试


def test_delete_removes_job(client):
    c, f = client
    b = c.post("/jobs", json={"type": "url", "source": "http://x/a.bin"}).json()
    assert c.delete(f"/jobs/{b['id']}").status_code == 200
    assert c.get(f"/jobs/{b['id']}").status_code == 404


def test_panel_served_without_auth(monkeypatch):
    # 即便设了 token，面板页 `/` 也必须可访问（否则没法加载页面输入 token）。
    monkeypatch.setattr(main.settings, "gateway_api_token", "secret")
    f = FakeAria2()
    app.dependency_overrides[get_aria2] = lambda: f
    with TestClient(app) as c:
        r = c.get("/")
        assert r.status_code == 200
        assert "HE 下载中心" in r.text
    app.dependency_overrides.clear()


def test_sse_accepts_token_query(monkeypatch):
    # EventSource 不能加请求头，靠 ?token= 鉴权。
    monkeypatch.setattr(main.settings, "gateway_api_token", "secret")
    f = FakeAria2()
    app.dependency_overrides[get_aria2] = lambda: f
    with TestClient(app) as c:
        assert c.get("/jobs").status_code == 401
        assert c.get("/jobs?token=secret").status_code == 200
    app.dependency_overrides.clear()


def test_batch_creates_one_grouped_job(client):
    c, f = client
    r = c.post("/jobs/batch", json={
        "name": "某漫画作品", "dest_dir": "/mnt/hdd/manga/book1",
        "files": [
            {"url": "http://x/001.jpg", "rel_path": "001.jpg", "headers": {"Cookie": "k=v"}},
            {"url": "http://x/002.jpg", "rel_path": "002.jpg"},
        ],
    })
    assert r.status_code == 200
    b = r.json()
    assert b["type"] == "batch" and b["name"] == "某漫画作品"
    # 两个文件各入队一次
    assert len(f.added) == 2
    # 第一文件带 cookie 头 + dir 落在 dest_dir
    _, opts0 = f.added[0]
    assert opts0["dir"] == "/mnt/hdd/manga/book1"
    assert opts0["out"] == "001.jpg"
    assert "Cookie: k=v" in opts0["header"]
    # 列表里只有一张卡（不是两行）
    assert len(c.get("/jobs").json()) == 1


def test_batch_aggregates_progress(client):
    c, f = client
    b = c.post("/jobs/batch", json={"name":"g","dest_dir":"/d","files":[
        {"url":"http://x/a","rel_path":"a"},{"url":"http://x/b","rel_path":"b"}]}).json()
    asyncio.run(main._reconcile_once(f))
    g = c.get(f"/jobs/{b['id']}").json()
    # 两文件各 250/1000 → 合计 500/2000 = 25%
    assert g["total_bytes"] == 2000 and g["completed_bytes"] == 500
    assert g["progress"] == 25.0 and g["status"] == "active"


def test_batch_cancel_then_delete(client):
    c, f = client
    b = c.post("/jobs/batch", json={"name":"g","dest_dir":"/d","files":[
        {"url":"http://x/a","rel_path":"a"},{"url":"http://x/b","rel_path":"b"}]}).json()
    assert c.post(f"/jobs/{b['id']}/cancel").json()["status"] == "canceled"
    assert ("remove", "gid-001") in f.controls and ("remove", "gid-002") in f.controls
    assert c.delete(f"/jobs/{b['id']}").status_code == 200
    assert c.get("/jobs").json() == []


def test_batch_view_exposes_per_file_progress(client):
    c, f = client
    b = c.post("/jobs/batch", json={"name": "g", "dest_dir": "/d", "files": [
        {"url": "http://x/a.jpg", "rel_path": "a.jpg"},
        {"url": "http://x/sub/b.jpg", "rel_path": "sub/b.jpg"}]}).json()
    asyncio.run(main._reconcile_once(f))
    g = c.get(f"/jobs/{b['id']}").json()
    assert len(g["files"]) == 2
    f0 = g["files"][0]
    assert f0["name"] == "a.jpg" and f0["rel_path"] == "a.jpg"
    assert f0["total_bytes"] == 1000 and f0["completed_bytes"] == 250
    assert f0["progress"] == 25.0 and f0["status"] == "active"
    # 嵌套路径的子文件显示名取末段
    assert g["files"][1]["name"] == "b.jpg"
    # 单文件任务的 files 仍为空
    s = c.post("/jobs", json={"type": "url", "source": "http://x/a.bin"}).json()
    assert c.get(f"/jobs/{s['id']}").json()["files"] == []


def test_claim_callback_is_idempotent():
    # 同一 job 只能被认领一次（防止多拍对账重复回调）。
    jid = store.create(type="url", source="http://x/a", dest_dir="/d",
                       filename=None, headers=None, callback_url="http://cb/")
    assert store.claim_callback(jid) is True
    assert store.claim_callback(jid) is False


class _FakeHttp:
    """记录 callback POST 的假 http 客户端。"""
    def __init__(self):
        self.posted = []

    async def post(self, url, json=None, timeout=None):
        self.posted.append((url, json))
        class R:
            status_code = 200
        return R()


async def _reconcile_and_drain(f):
    """跑一拍对账，并把它 create_task 出去的回调全部 await 完。"""
    await main._reconcile_once(f)
    for t in list(main._callback_tasks):
        await t


def test_callback_fires_on_complete(client):
    c, f = client
    http = _FakeHttp()
    main.app.state.http = http
    b = c.post("/jobs", json={"type": "url", "source": "http://x/a.bin",
                              "callback_url": "http://cb/done"}).json()
    f.done = True
    asyncio.run(_reconcile_and_drain(f))
    assert c.get(f"/jobs/{b['id']}").json()["status"] == "complete"
    assert len(http.posted) == 1
    url, body = http.posted[0]
    assert url == "http://cb/done"
    assert body["event"] == "complete" and body["job"]["id"] == b["id"]
    # 再对账一拍不应重复回调（已认领）
    asyncio.run(_reconcile_and_drain(f))
    assert len(http.posted) == 1


def test_callback_fires_for_batch_with_files(client):
    c, f = client
    http = _FakeHttp()
    main.app.state.http = http
    b = c.post("/jobs/batch", json={"name": "g", "dest_dir": "/d",
        "callback_url": "http://cb/b", "files": [
            {"url": "http://x/a", "rel_path": "a"},
            {"url": "http://x/b", "rel_path": "b"}]}).json()
    f.done = True
    asyncio.run(_reconcile_and_drain(f))
    assert c.get(f"/jobs/{b['id']}").json()["status"] == "complete"
    assert len(http.posted) == 1
    _, body = http.posted[0]
    assert body["event"] == "complete"
    assert len(body["job"]["files"]) == 2  # 回调载荷带上逐文件明细


def test_no_callback_without_url(client):
    c, f = client
    http = _FakeHttp()
    main.app.state.http = http
    b = c.post("/jobs", json={"type": "url", "source": "http://x/a.bin"}).json()
    f.done = True
    asyncio.run(_reconcile_and_drain(f))
    assert c.get(f"/jobs/{b['id']}").json()["status"] == "complete"
    assert http.posted == []


def test_he_sync_proxies_with_stored_creds(client, monkeypatch):
    c, f = client
    monkeypatch.setattr(main.settings, "he_manager_url", "http://he:8000")
    calls = {"get": [], "post": []}

    class _Resp:
        def __init__(self, data, status=200):
            self._data = data; self.status_code = status
        def raise_for_status(self): pass
        def json(self): return self._data

    class FakeHeHttp:
        async def get(self, url, params=None, headers=None, timeout=None):
            calls["get"].append(url)
            return _Resp([{"id": 7, "source_type": "wnacg", "favorites_url": "http://w/fav"}])
        async def post(self, url, json=None, headers=None, timeout=None):
            calls["post"].append((url, json))
            return _Resp({"synced_count": 5, "source": {}, "items": []})

    main.app.state.http = FakeHeHttp()
    r = c.post("/he/sync", json={"source_type": "wnacg"})
    assert r.status_code == 200
    assert r.json()["synced_count"] == 5
    # 转发到 HE 的 wnacg sync，只带 source_id + 地址（复用已存 cookie，不传凭据）
    url, body = calls["post"][0]
    assert url.endswith("/external/wnacg/sync")
    assert body == {"source_id": 7, "favorites_url": "http://w/fav"}
    assert "cookie" not in body


def test_he_sync_unconfigured_source(client, monkeypatch):
    c, f = client
    monkeypatch.setattr(main.settings, "he_manager_url", "http://he:8000")

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return []   # HE 里没有任何来源

    class FakeHeHttp:
        async def get(self, *a, **k): return _Resp()
        async def post(self, *a, **k): raise AssertionError("不该走到 sync")

    main.app.state.http = FakeHeHttp()
    r = c.post("/he/sync", json={"source_type": "asmr"})
    assert r.status_code == 400
    assert "还没配置" in r.json()["detail"]


def test_auth_enforced(monkeypatch):
    monkeypatch.setattr(main.settings, "gateway_api_token", "secret")
    f = FakeAria2()
    app.dependency_overrides[get_aria2] = lambda: f
    with TestClient(app) as c:
        assert c.get("/jobs").status_code == 401
        assert c.get("/jobs", headers={"Authorization": "Bearer secret"}).status_code == 200
    app.dependency_overrides.clear()
