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
        self.gid_seq = 0

    async def add_uri(self, uris, options=None):
        self.added.append((uris, options or {}))
        self.gid_seq += 1
        return f"gid-{self.gid_seq:03d}"

    def _status(self, gid):
        return {
            "gid": gid, "status": "active",
            "totalLength": "1000", "completedLength": "250", "downloadSpeed": "500",
            "dir": "/downloads",
            "files": [{"path": "/downloads/100MB.bin", "length": "1000", "completedLength": "250"}],
        }

    async def tell_active(self):
        return [self._status(f"gid-{self.gid_seq:03d}")] if (self.present and self.gid_seq) else []

    async def tell_waiting(self, *a):
        return []

    async def tell_stopped(self, *a):
        return []

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


def test_auth_enforced(monkeypatch):
    monkeypatch.setattr(main.settings, "gateway_api_token", "secret")
    f = FakeAria2()
    app.dependency_overrides[get_aria2] = lambda: f
    with TestClient(app) as c:
        assert c.get("/jobs").status_code == 401
        assert c.get("/jobs", headers={"Authorization": "Bearer secret"}).status_code == 200
    app.dependency_overrides.clear()
