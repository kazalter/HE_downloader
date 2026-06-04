"""M1 冒烟测试：用假的 aria2 客户端验证 gateway 路由 / 序列化 / 选项拼装，
不依赖真实 aria2 进程。"""
from __future__ import annotations

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.main import app, get_aria2  # noqa: E402


class FakeAria2:
    """记录调用、返回可控数据。"""

    def __init__(self):
        self.added: list[tuple[list[str], dict]] = []
        self.controls: list[tuple[str, str]] = []

    async def add_uri(self, uris, options=None):
        self.added.append((uris, options or {}))
        return "gid-001"

    async def tell_status(self, gid):
        return {
            "gid": gid,
            "status": "active",
            "totalLength": "1000",
            "completedLength": "250",
            "downloadSpeed": "500",
            "dir": "/downloads",
            "files": [{"path": "/downloads/100MB.bin", "length": "1000", "completedLength": "250"}],
        }

    async def tell_active(self):
        return [await self.tell_status("gid-001")]

    async def tell_waiting(self, offset=0, num=200):
        return []

    async def tell_stopped(self, offset=0, num=200):
        return []

    async def pause(self, gid):
        self.controls.append(("pause", gid))
        return gid

    async def unpause(self, gid):
        self.controls.append(("unpause", gid))
        return gid

    async def remove(self, gid):
        self.controls.append(("remove", gid))
        return gid

    async def get_global_stat(self):
        return {"downloadSpeed": "500", "uploadSpeed": "0", "numActive": "1", "numWaiting": "0", "numStopped": "0"}

    async def get_version(self):
        return {"version": "1.36.0"}


@pytest.fixture
def fake():
    f = FakeAria2()
    app.dependency_overrides[get_aria2] = lambda: f
    with TestClient(app) as client:
        yield client, f
    app.dependency_overrides.clear()


def test_health(fake):
    client, _ = fake
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["aria2"] == "1.36.0"


def test_create_url_job(fake):
    client, f = fake
    r = client.post("/jobs", json={"type": "url", "source": "http://x/100MB.bin"})
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "gid-001"
    assert body["progress"] == 25.0
    assert body["name"] == "100MB.bin"
    # 直链不应带 seed-time
    uris, options = f.added[0]
    assert uris == ["http://x/100MB.bin"]
    assert options["dir"] == "/downloads"
    assert "seed-time" not in options


def test_create_magnet_sets_no_seeding(fake):
    client, f = fake
    r = client.post("/jobs", json={"type": "magnet", "source": "magnet:?xt=urn:btih:abc"})
    assert r.status_code == 200
    _, options = f.added[0]
    assert options["seed-time"] == "0"  # 下完即停、不做种


def test_headers_and_dest_dir(fake):
    client, f = fake
    client.post("/jobs", json={
        "type": "url", "source": "http://x/a.jpg",
        "dest_dir": "manga/book1", "filename": "001.jpg",
        "headers": {"Referer": "http://x", "Cookie": "k=v"},
    })
    _, options = f.added[0]
    assert options["dir"] == os.path.join("/downloads", "manga/book1")
    assert options["out"] == "001.jpg"
    assert "Referer: http://x" in options["header"]


def test_list_and_control(fake):
    client, f = fake
    assert len(client.get("/jobs").json()) == 1
    assert client.post("/jobs/gid-001/pause").status_code == 200
    assert client.post("/jobs/gid-001/cancel").status_code == 200
    assert ("pause", "gid-001") in f.controls
    assert ("remove", "gid-001") in f.controls


def test_auth_enforced(monkeypatch):
    from app import config, main
    monkeypatch.setattr(config.settings, "gateway_api_token", "secret")
    monkeypatch.setattr(main.settings, "gateway_api_token", "secret")
    f = FakeAria2()
    app.dependency_overrides[get_aria2] = lambda: f
    with TestClient(app) as client:
        assert client.get("/jobs").status_code == 401
        ok = client.get("/jobs", headers={"Authorization": "Bearer secret"})
        assert ok.status_code == 200
    app.dependency_overrides.clear()
