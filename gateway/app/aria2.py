"""极薄的 aria2 JSON-RPC 客户端。

只封装 gateway 当前用到的方法；引擎本身的并发/续传/限速都交给 aria2，
这里不重复造轮子。所有调用都会自动带上 `token:<secret>` 作为第一个参数。
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx


class Aria2Error(RuntimeError):
    """aria2 返回的 JSON-RPC error 对象。"""


class Aria2Client:
    def __init__(self, rpc_url: str, secret: str, http: httpx.AsyncClient):
        self._url = rpc_url
        self._secret = secret
        self._http = http

    async def _call(self, method: str, *params: Any) -> Any:
        full_params: list[Any] = []
        if self._secret:
            full_params.append(f"token:{self._secret}")
        full_params.extend(params)
        body = {
            "jsonrpc": "2.0",
            "id": uuid4().hex,
            "method": method,
            "params": full_params,
        }
        resp = await self._http.post(self._url, json=body)
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            err = data["error"]
            raise Aria2Error(f"aria2 {method} 失败: {err.get('message')} (code={err.get('code')})")
        return data.get("result")

    # --- 写 ---
    async def add_uri(self, uris: list[str], options: dict | None = None) -> str:
        """返回新任务的 gid。"""
        return await self._call("aria2.addUri", uris, options or {})

    async def pause(self, gid: str) -> str:
        return await self._call("aria2.pause", gid)

    async def unpause(self, gid: str) -> str:
        return await self._call("aria2.unpause", gid)

    async def remove(self, gid: str) -> str:
        return await self._call("aria2.remove", gid)

    # --- 读 ---
    async def tell_status(self, gid: str) -> dict:
        return await self._call("aria2.tellStatus", gid)

    async def tell_active(self) -> list[dict]:
        return await self._call("aria2.tellActive")

    async def tell_waiting(self, offset: int = 0, num: int = 200) -> list[dict]:
        return await self._call("aria2.tellWaiting", offset, num)

    async def tell_stopped(self, offset: int = 0, num: int = 200) -> list[dict]:
        return await self._call("aria2.tellStopped", offset, num)

    async def get_global_stat(self) -> dict:
        return await self._call("aria2.getGlobalStat")

    async def get_version(self) -> dict:
        return await self._call("aria2.getVersion")
