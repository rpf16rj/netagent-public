"""AdGuard Home (CT109 10.3.3.2:3002) — stats via API /control/*.

Auth: HTTP basic (usuario/senha do webui). Cert e self-signed ->
verify=False. Cache de 30s p/ GET /api/adguard — nunca derruba o backend.
"""
import asyncio
import logging
import time
from typing import Any, Dict, Optional

import httpx

log = logging.getLogger("netagent.adguard")

_TIMEOUT_S = 6.0


class AdGuardClient:
    def __init__(self, base_url: str, user: str = "", password: str = "",
                 timeout: float = _TIMEOUT_S) -> None:
        self.base = base_url.rstrip("/")
        self.auth = (user, password) if user else None
        self.timeout = timeout

    async def poll(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"ts": time.time(), "reachable": False}
        try:
            async with httpx.AsyncClient(
                    timeout=self.timeout, verify=False,
                    auth=self.auth) as c:
                st = await c.get(self.base + "/control/status")
                if st.status_code != 200:
                    out["error"] = "status %s" % st.status_code
                    return out
                stats = await c.get(self.base + "/control/stats")
                sj = st.json()
                dj = stats.json() if stats.status_code == 200 else {}
                out.update({
                    "reachable": True,
                    "version": sj.get("version"),
                    "protection_enabled":
                        not sj.get("protection_enabled") is False,
                    "dns_port": sj.get("dns_port"),
                    "queries_24h": dj.get("num_dns_queries"),
                    "blocked_24h": dj.get("num_blocked_filtering"),
                    "blocked_pct": round(
                        (dj.get("num_blocked_filtering") or 0) /
                        max(1, dj.get("num_dns_queries") or 1) * 100, 1),
                    "avg_ms": round((dj.get("avg_processing_time") or 0)
                                    * 1000, 1),
                    "top_domains": dj.get("top_queried_domains") or [],
                    "top_blocked": dj.get("top_blocked_domains") or [],
                    "top_clients": dj.get("top_clients") or [],
                })
                return out
        except httpx.HTTPError as exc:
            out["error"] = "unreachable"
            out["note"] = str(exc)[:120]
            return out
        except Exception as exc:  # nunca propagar
            log.exception("poll adguard falhou")
            out["error"] = "internal"
            out["note"] = str(exc)[:120]
            return out


class AdGuardPoller:
    """Loop async — consulta o AdGuard a cada `interval` e cacheia."""

    def __init__(self, client: AdGuardClient,
                 interval: float = 30.0) -> None:
        self.client = client
        self.interval = max(10.0, interval)
        self.latest: Dict[str, Any] = {
            "ts": 0, "reachable": False,
        }
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="adguard")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.latest = await self.client.poll()
            except Exception:
                log.exception("adguard poll falhou")
            try:
                await asyncio.wait_for(self._stop.wait(),
                                       timeout=self.interval)
            except asyncio.TimeoutError:
                pass
