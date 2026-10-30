"""Poller — ciclo de coleta a cada POLL_INTERVAL nos dois roteadores.

Coleta por roteador (via RosClient):
  - /interface/print         -> contadores rx/tx -> bps por delta
  - /ip/route/print          -> rotas default + flag disabled (estado do link)
  - /ip/dhcp-server/lease/print
  - /system/resource/print   -> cpu-load, mem, uptime
  - /system/health/print     -> sensores (se houver)
  - /ping gw do WAN          -> RTT do link

Roteador que falha vira {"reachable": False} no snapshot — o poller NUNCA
para por causa de um roteador fora do ar.

Persiste sample cru no SQLite, alimenta o AlertEngine e publica o snapshot
no Broadcaster (SSE). Um loop paralelo faz rollup de 1min e limpeza.
"""
import asyncio
import ipaddress
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from .alerts import AlertEngine
from .config import RouterTarget, Settings
from .db import Database
from .ros_client import RosClient, RouterUnreachable
from .stream import Broadcaster

log = logging.getLogger("netagent.poller")

_ROLLUP_INTERVAL_S = 60.0
_CLEANUP_INTERVAL_S = 3600.0
_BASELINE_INTERVAL_S = 1800.0   # recalcula baselines de 7d a cada ~30min


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _norm_system(raw: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "cpu_load": _int(raw.get("cpu-load")),
        "free_memory": _int(raw.get("free-memory")),
        "total_memory": _int(raw.get("total-memory")),
        "uptime": raw.get("uptime", ""),
        "version": raw.get("version", ""),
        "board_name": raw.get("board-name", ""),
        "architecture": raw.get("architecture-name", ""),
    }


def _is_default_route(route: Dict[str, Any]) -> bool:
    return route.get("dst-address") in ("0.0.0.0/0", "::/0")


# comment -> ISP. Cobre as convencoes novas (LINK REDEBR / LINK ONDA-LIVRE)
# e antigas (LINK 1 #3 / LINK 2 #6). Rotas de teste das VRFs tem comment
# "!==" e/ou routing-table TEST-* e sao filtradas antes.
_LINK_TOKENS = (
    ("REDEBR", "RedeBR"),
    ("LINK 2", "RedeBR"),
    ("ONDA-LIVRE", "Onda Livre"),
    ("ONDALIVRE", "Onda Livre"),
    ("ONDA LIVRE", "Onda Livre"),
    ("LINK 1", "Onda Livre"),
)


def _classify_isp(comment: str) -> Optional[str]:
    c = comment.upper()
    for token, isp in _LINK_TOKENS:
        if token in c:
            return isp
    return None


class Poller:
    def __init__(self, settings: Settings, db: Database,
                 broadcaster: Broadcaster,
                 alert_engine: Optional[AlertEngine] = None,
                 ipfix: Any = None) -> None:
        self.settings = settings
        self.db = db
        self.broadcaster = broadcaster
        self.alerts = alert_engine
        self.ipfix = ipfix              # IpfixCollector — alimenta ip_1m
        self.targets: Dict[str, RouterTarget] = {
            t.name: t for t in settings.routers()}
        self.clients: Dict[str, RosClient] = {
            name: RosClient(t, settings) for name, t in self.targets.items()}
        # router -> rede da LAN (atribuicao de IPs do IPFIX p/ ip_1m)
        self._lan_nets: Dict[str, Any] = {}
        for name, t in self.targets.items():
            try:
                self._lan_nets[name] = ipaddress.ip_network(t.lan_subnet)
            except ValueError:
                log.warning("lan_subnet invalida p/ %s: %s",
                            name, t.lan_subnet)
        self._prev_counters: Dict[str, Dict[str, Tuple[float, int, int]]] = {
            name: {} for name in self.targets}
        self._task: Optional[asyncio.Task] = None
        self._rollup_task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self.latest_snapshot: Dict[str, Any] = {"ts": 0, "routers": {}}

    # ---------- ciclo ----------

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="poller")
        self._rollup_task = asyncio.create_task(
            self._rollup_loop(), name="rollup")

    async def stop(self) -> None:
        self._stop.set()
        for t in (self._task, self._rollup_task):
            if t is not None:
                t.cancel()

    async def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                snapshot = await self.poll_once()
                self.latest_snapshot = snapshot
                self.broadcaster.publish("snapshot", snapshot)
                if self.alerts is not None:
                    fired = self.alerts.evaluate(snapshot)
                    if fired:
                        await self.alerts.dispatch(
                            fired, db=self.db, broadcaster=self.broadcaster)
            except Exception:
                log.exception("ciclo de poll falhou")
            elapsed = time.monotonic() - started
            wait = max(0.5, self.settings.poll_interval - elapsed)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass

    async def poll_once(self) -> Dict[str, Any]:
        results = await asyncio.gather(
            *(self._poll_router(name) for name in self.targets),
            return_exceptions=True)
        snap = {"ts": time.time(), "routers": {}}
        for name, res in zip(self.targets, results):
            if isinstance(res, Exception):
                snap["routers"][name] = {
                    "reachable": False,
                    "error": str(res),
                    "wan_interface": self.targets[name].wan_interface,
                    "isp": self.targets[name].isp_name,
                    "links": [], "interfaces": {}, "leases": [],
                    "system": {},
                }
            else:
                snap["routers"][name] = res
        return snap

    # ---------- por roteador ----------

    async def _poll_router(self, name: str) -> Dict[str, Any]:
        target = self.targets[name]
        client = self.clients[name]
        now = time.time()
        try:
            ifaces_raw = await client.get_interfaces()
            routes_raw = await client.get_routes()
            leases = await client.get_dhcp_leases()
            system = _norm_system(await client.get_system_resource())
            health = await client.get_system_health()
            system["conn_count"] = await client.get_conn_count()
        except RouterUnreachable as exc:
            return {"reachable": False, "error": str(exc),
                    "wan_interface": target.wan_interface,
                    "isp": target.isp_name, "links": [], "interfaces": {},
                    "leases": [], "system": {}}

        interfaces = self._derive_rates(name, ifaces_raw, now)
        links = self._derive_links(target, routes_raw)

        # RTT do gateway do WAN local (mede a saida pelo link local;
        # so grava no link cujo gateway e o WAN local deste roteador)
        try:
            p = await client.ping(target.wan_gateway,
                                  count=self.settings.ping_count)
            rtt = p.get("rtt_ms")
            loss = None
            if p.get("sent"):
                loss = round(100.0 * (1 - p.get("received", 0) / p["sent"]), 1)
            for link in links:
                if link.get("gateway") == target.wan_gateway:
                    link["rtt_ms"] = rtt
                    link["loss_pct"] = loss
        except Exception as exc:
            log.debug("ping gw %s de %s falhou: %s",
                      target.wan_gateway, name, exc)

        rsnap = {
            "reachable": True,
            "label": target.label,
            "isp": target.isp_name,
            "lan_subnet": target.lan_subnet,
            "wan_interface": target.wan_interface,
            "system": system,
            "health": health,
            "interfaces": interfaces,
            "links": links,
            "leases": leases,
        }
        # WAN throughput = soma das interfaces unicamente associadas aos
        # links (out_interface) — cobre links "via fibra" cujo trafego
        # corre pela interface de transito, nao pela wan_interface fisica.
        link_ifaces = {l.get("out_interface") for l in links
                       if l.get("out_interface")} \
            or {target.wan_interface}
        await self.db.insert_sample(name, {
            "system": system, "links": links,
            "wan_rx_bps": sum(
                (interfaces.get(i) or {}).get("rx_bps") or 0
                for i in link_ifaces) or None,
            "wan_tx_bps": sum(
                (interfaces.get(i) or {}).get("tx_bps") or 0
                for i in link_ifaces) or None,
            "lease_count": len(leases),
            # uptime por link (Monitoring v2): 1 = rota habilitada — o
            # rollup de 1min tira a media -> link_up_<i> em samples_1m
            "links_up": {str(i): (0 if l.get("disabled") else 1)
                         for i, l in enumerate(links)},
            # rates por link (iface de saida real de cada rota) — chart
            # de cada card usa sua propria serie, nao a WAN total
            "links_rates": {
                str(i): {
                    "rx": (interfaces.get(l.get("out_interface") or "")
                           or {}).get("rx_bps"),
                    "tx": (interfaces.get(l.get("out_interface") or "")
                           or {}).get("tx_bps"),
                    "rtt": l.get("rtt_ms"),
                } for i, l in enumerate(links)
            },
        }, ts=now)

        # tracking de dispositivos: upsert por MAC a cada ciclo; evento
        # somente p/ MAC inserido agora (dispositivo novo de verdade —
        # persistente, nao inunda em restart nem repete lease conhecido)
        try:
            new_macs = await self.db.upsert_devices([
                {"mac": l.get("mac-address"),
                 "ip": l.get("address"),
                 "name": l.get("host-name") or l.get("comment") or "",
                 "router": name}
                for l in leases if l.get("mac-address")
            ], ts=now)
            by_mac = {l.get("mac-address"): l for l in leases}
            for mac in sorted(new_macs):
                l = by_mac.get(mac) or {}
                who = l.get("host-name") or l.get("comment") or ""
                await self.db.insert_event(
                    "info" if who else "warning", "agent",
                    "Novo dispositivo em %s: %s (%s)%s"
                    % (name, l.get("address", "?"), who or mac,
                       "" if who else " — DISPOSITIVO DESCONHECIDO"))
        except Exception:
            log.exception("upsert_devices falhou (%s)", name)
        return rsnap

    def _derive_rates(self, name: str,
                      ifaces: List[Dict[str, Any]],
                      now: float) -> Dict[str, Dict[str, Any]]:
        """bps por delta de contadores. Primeiro ciclo devolve bps=None."""
        prev = self._prev_counters[name]
        out: Dict[str, Dict[str, Any]] = {}
        for i in ifaces:
            iname = i.get("name")
            if not iname:
                continue
            rx = _int(i.get("rx-byte"))
            tx = _int(i.get("tx-byte"))
            entry: Dict[str, Any] = {
                "rx_bytes": rx, "tx_bytes": tx,
                "running": i.get("running") == "true" or i.get("running") is True,
                "type": i.get("type", ""),
                "rx_bps": None, "tx_bps": None,
            }
            old = prev.get(iname)
            if old is not None:
                dt = now - old[0]
                if dt > 0:
                    # contadores de 64 bits; wrap e tratado como delta positivo
                    entry["rx_bps"] = max(0.0, (rx - old[1]) * 8.0 / dt)
                    entry["tx_bps"] = max(0.0, (tx - old[2]) * 8.0 / dt)
            prev[iname] = (now, rx, tx)
            out[iname] = entry
        return out

    def _derive_links(self, target: RouterTarget,
                      routes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Rotas default da tabela MAIN = estado dos links.

        Cada MK carrega rotas dos DOIS ISPs (local + via transito), entao o
        ISP vem do comment (tokens globais), nao do roteador. Rotas de VRFs
        de teste (routing-table != main ou comment '!==') sao excluidas.
        """
        links = []
        for r in routes:
            if not _is_default_route(r):
                continue
            table = r.get("routing-table") or "main"
            comment = r.get("comment") or ""
            if table != "main" or comment.startswith("!=="):
                continue
            gateway = r.get("gateway", "")
            via_transit = gateway.startswith("172.20.20.")
            # immediate-neighbor vem vazio p/ rotas via gateway -> deriva
            # a iface de saida pelo gateway (WAN local ou fibra de transito)
            out_iface = (target.transit_interface if via_transit
                         else target.wan_interface)
            links.append({
                "isp": _classify_isp(comment) or target.isp_name,
                "route_comment": comment,
                "gateway": gateway,
                "out_interface": out_iface,
                "via_transit": via_transit,
                "disabled": r.get("disabled") in ("true", True),
                "active": r.get("active") in ("true", True),
                "distance": _int(r.get("distance"), 1),
                "rtt_ms": None,
                "loss_pct": None,
            })
        return links

    # ---------- rollup + retencao ----------

    async def _rollup_loop(self) -> None:
        last_cleanup = 0.0
        last_baseline = 0.0
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(),
                                       timeout=_ROLLUP_INTERVAL_S)
                break
            except asyncio.TimeoutError:
                pass
            try:
                await self._make_rollups()
            except Exception:
                log.exception("rollup falhou")
            try:
                await self._make_ip_rollups()
            except Exception:
                log.exception("ip_1m rollup falhou")
            if time.time() - last_baseline > _BASELINE_INTERVAL_S:
                last_baseline = time.time()
                try:
                    n = await self.db.compute_baselines(list(self.targets))
                    if n:
                        log.info("baselines recalculados: %d linhas", n)
                except Exception:
                    log.exception("compute_baselines falhou")
            if time.time() - last_cleanup > _CLEANUP_INTERVAL_S:
                last_cleanup = time.time()
                try:
                    dropped = await self.db.cleanup()
                    if any(dropped.values()):
                        log.info("retencao: %s", dropped)
                except Exception:
                    log.exception("cleanup falhou")

    async def _make_rollups(self) -> None:
        """Media dos samples crus do ultimo minuto -> samples_1m."""
        now_min = int(time.time()) // 60 * 60
        for name in self.targets:
            last = await self.db.last_rollup_ts(name)
            if last is not None and last >= now_min - 60:
                continue  # ja rolou esse minuto
            rows = await self.db.samples_since(name, now_min - 120)
            rows = [r for r in rows if now_min - 120 <= r["ts"] < now_min]
            if not rows:
                continue
            cpus = [r["metric"].get("system", {}).get("cpu_load")
                    for r in rows]
            cpus = [c for c in cpus if c is not None]
            rx = [r["metric"].get("wan_rx_bps") for r in rows]
            rx = [v for v in rx if v is not None]
            tx = [r["metric"].get("wan_tx_bps") for r in rows]
            tx = [v for v in tx if v is not None]
            rtts = [l.get("rtt_ms") for r in rows
                    for l in r["metric"].get("links", [])
                    if l.get("rtt_ms") is not None]
            metric: Dict[str, Any] = {
                "cpu_avg": sum(cpus) / len(cpus) if cpus else None,
                "wan_rx_bps_avg": sum(rx) / len(rx) if rx else None,
                "wan_tx_bps_avg": sum(tx) / len(tx) if tx else None,
                "rtt_ms_avg": sum(rtts) / len(rtts) if rtts else None,
                "sample_count": len(rows),
            }
            # uptime por link: media de links_up[str(i)] -> link_up_<i>
            up_keys = sorted(
                {k for r in rows
                 for k in r["metric"].get("links_up", {})},
                key=lambda k: (len(k), k))
            for k in up_keys:
                vals = [r["metric"].get("links_up", {}).get(k)
                        for r in rows]
                vals = [v for v in vals if v is not None]
                if vals:
                    metric["link_up_%s" % k] = sum(vals) / len(vals)
            # rates por link: links_rates[str(i)].{rx,tx,rtt} -> medias
            rate_keys = sorted(
                {k for r in rows
                 for k in r["metric"].get("links_rates", {})},
                key=lambda k: (len(k), k))
            for k in rate_keys:
                for fld in ("rx", "tx", "rtt"):
                    vals = [(r["metric"].get("links_rates", {})
                             .get(k) or {}).get(fld) for r in rows]
                    vals = [v for v in vals if v is not None]
                    if vals:
                        metric["link_%s_%s" % (fld, k)] = \
                            sum(vals) / len(vals)
            await self.db.insert_rollup(name, metric, ts_min=now_min - 60)

    # ---------- ip_1m (rates por host do IPFIX — Monitoring v2) ----------

    def _router_for_ip(self, ip: str) -> Optional[str]:
        """IP local -> roteador dono da LAN (por lan_subnet)."""
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return None
        for name, net in self._lan_nets.items():
            if addr in net:
                return name
        return None

    async def _make_ip_rollups(self) -> None:
        """Persiste rates da janela de 60s do IPFIX -> ip_1m (~1/min).

        Top ~30 IPs por roteador (por total_bps) — suficiente p/ sparklines
        dos top talkers sem inchar a tabela.
        """
        if self.ipfix is None:
            return
        top = self.ipfix.top_talkers(window_s=60,
                                     limit=30 * max(1, len(self.targets)))
        if not top:
            return
        ts_min = int(time.time()) // 60 * 60
        per_router: Dict[str, int] = {}
        rows = []
        for t in top:
            router = self._router_for_ip(t["ip"])
            if router is None or per_router.get(router, 0) >= 30:
                continue
            per_router[router] = per_router.get(router, 0) + 1
            rows.append({"ts_min": ts_min, "router": router, "ip": t["ip"],
                         "rx_bps": t["rx_bps"], "tx_bps": t["tx_bps"]})
        if rows:
            await self.db.insert_ip_rollup(rows)
