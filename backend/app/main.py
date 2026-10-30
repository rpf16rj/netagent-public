"""netagent backend — FastAPI app.

Endpoints:
  GET  /api/health
  GET  /api/snapshot            ultimo estado completo do poller
  GET  /api/links               status/throughput/rtt por link
  GET  /api/hosts/top           top talkers locais agregados do IPFIX
  GET  /api/leases              leases DHCP dos dois MKs
  GET  /api/events              timeline de eventos/alertas
  GET  /api/history             rollups de 1min p/ graficos (?hours=1..168)
  GET  /api/devices             devices conhecidos (?new_days=7 -> is_new)
  GET  /api/devices/stats       contagens (total/novos/vistos-24h/por MK)
  GET  /api/baseline            baselines 7d (median/p95/avg) por MK+metrica
  GET  /api/uptime              uptime por link (media link_up_<i>) ?days=1..30
  GET  /api/healthscore         score 0-100 por roteador/link vs baseline
  GET  /api/heatmap             matriz dia x hora de wan_rx_bps_avg ?days=7
  GET  /api/talkers/spark       serie ip_1m por IP ?ips=a,b&hours=1
  GET  /api/wifi                cache do AP Tenda RX2 Pro (Casa User)
  GET  /api/actions             lista + templates disponiveis
  POST /api/actions             cria proposta {kind, router, params}
  POST /api/actions/{id}/approve|reject
  GET  /api/stream              SSE (snapshot + alert + action)
  GET  /api/agent/status        estado do agente/kill-switch
  POST /api/agent/ask           Q&A sob demanda {question} -> {answer}
  POST /api/agent/killswitch    liga/desliga read-only {read_only: bool}
  GET  /api/ipfix/stats         contadores do coletor IPFIX
"""
import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .actions import REGISTRY, ActionEngine, ParamError
from .agent import AgentBrain
from .alerts import AlertEngine
from .config import get_settings
from .db import Database
from .ipfix import IpfixCollector
from .poller import Poller
from .stream import Broadcaster, format_sse
from .adguard import AdGuardClient, AdGuardPoller
from .wifi import TendaClient, WifiPoller

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("netagent.main")

settings = get_settings()
db = Database(settings.db_path,
              sample_retention_hours=settings.sample_retention_hours,
              rollup_retention_days=settings.rollup_retention_days)
broadcaster = Broadcaster()
alert_engine = AlertEngine(ntfy_url=settings.ntfy_url,
                           dedup_minutes=settings.alert_dedup_minutes,
                           ntfy_enabled=settings.ntfy_enabled,
                           min_severity=settings.ntfy_min_severity)
ipfix = IpfixCollector(settings.ipfix_listen_host,
                       settings.ipfix_listen_port,
                       lan_subnets=[s.strip()
                                    for s in settings.lan_subnets.split(",")
                                    if s.strip()])
poller = Poller(settings, db, broadcaster, alert_engine, ipfix=ipfix)
actions = ActionEngine(settings, db, poller.clients, broadcaster,
                       alerts=alert_engine)
agent_brain = AgentBrain(settings, db, poller, actions, broadcaster,
                         alert_engine, ipfix=ipfix)
wifi_poller = WifiPoller(
    TendaClient(settings.tenda_host, settings.tenda_pass),
    interval=settings.wifi_poll_interval)
adguard_poller = AdGuardPoller(
    AdGuardClient(settings.adguard_url, settings.adguard_user,
                  settings.adguard_pass),
    interval=settings.adguard_poll_interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    await db.insert_event("info", "agent", "netagent backend iniciado")
    await poller.start()
    await agent_brain.start()
    try:
        await ipfix.start()
    except OSError as exc:
        log.warning("IPFIX nao subiu (porta ocupada?): %s", exc)
    await wifi_poller.start()
    await adguard_poller.start()
    _speedtest_task = asyncio.create_task(_speedtest_scheduler())
    yield
    _speedtest_task.cancel()
    await poller.stop()
    await wifi_poller.stop()
    await adguard_poller.stop()
    await agent_brain.stop()
    await ipfix.stop()
    await db.close()


app = FastAPI(title="netagent", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------ speedtest
# speedtest-cli no venv mede a rota ATIVA do LXC (mk-b -> link ativo dele).
# Roda 1x/dia na janela 04-06h + endpoint manual. O teste satura o link
# por ~30s — por isso janela de madrugada; manual fica sob controle do
# usuario no dashboard.
_st_state: Dict[str, Any] = {"running": False, "last_ts": 0.0,
                             "last_error": None}
_ST_BIN = "/opt/netagent/venv/bin/speedtest-cli"


async def _run_speedtest(trigger: str) -> Dict[str, Any]:
    if _st_state["running"]:
        return {"ok": False, "note": "ja rodando"}
    _st_state["running"] = True
    try:
        proc = await asyncio.create_subprocess_exec(
            _ST_BIN, "--json", "--timeout", "60",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=150)
        import json as _json
        j = _json.loads(out.decode("utf-8", "replace"))
        rec = {"ts": time.time(),
               "dl_bps": j.get("download"),
               "ul_bps": j.get("upload"),
               "ping_ms": j.get("ping"),
               "server": (j.get("server") or {}).get("sponsor") or
                         (j.get("server") or {}).get("name") or "",
               "trigger": trigger}
        await db.insert_speedtest(rec["dl_bps"], rec["ul_bps"],
                                  rec["ping_ms"], rec["server"],
                                  trigger, rec["ts"])
        _st_state.update({"last_ts": rec["ts"], "last_error": None})
        await db.insert_event(
            "info", "agent",
            "speedtest %s: %.0fM down / %.0fM up / %.0fms (%s)" % (
                trigger, (rec["dl_bps"] or 0) / 1e6,
                (rec["ul_bps"] or 0) / 1e6, rec["ping_ms"] or 0,
                rec["server"]))
        return {"ok": True, **rec}
    except Exception as exc:
        _st_state["last_error"] = str(exc)[:200]
        log.warning("speedtest falhou: %s", exc)
        return {"ok": False, "note": str(exc)[:200]}
    finally:
        _st_state["running"] = False


async def _speedtest_scheduler() -> None:
    """Roda 1x/dia na janela 04:00-06:59 local (madruada, link ocioso)."""
    import datetime
    while True:
        try:
            await asyncio.sleep(600)
            last = (await db.latest_speedtests(1) or [{}])[0]
            recent = last.get("ts", 0) > time.time() - 20 * 3600
            if not recent and datetime.datetime.now().hour in (4, 5, 6):
                log.info("speedtest agendado disparando")
                await _run_speedtest("auto")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("speedtest scheduler falhou")


@app.get("/api/speedtest")
async def speedtest() -> Dict[str, Any]:
    latest = await db.latest_speedtests(10)
    return {"running": _st_state["running"],
            "last_error": _st_state["last_error"],
            "results": latest}


@app.post("/api/speedtest/run")
async def speedtest_run() -> Dict[str, Any]:
    if _st_state["running"]:
        return {"ok": False, "note": "ja rodando"}
    return await _run_speedtest("manual")


# ------------------------------------------------------------------ basic

@app.get("/api/health")
async def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "ts": time.time(),
        "routers": {n: c.connected for n, c in poller.clients.items()},
        "agent_read_only": actions.read_only,
        "ipfix_packets": ipfix.stats["packets"],
        "sse_subscribers": broadcaster.subscribers,
    }


@app.get("/api/snapshot")
async def snapshot() -> Dict[str, Any]:
    return poller.latest_snapshot


@app.get("/api/links")
async def links() -> Dict[str, Any]:
    """Status agregado dos links, achatado p/ o dashboard."""
    out = []
    snap = poller.latest_snapshot
    for name, rsnap in snap.get("routers", {}).items():
        ifaces = rsnap.get("interfaces", {})
        wan = rsnap.get("wan_interface", "")
        for link in rsnap.get("links", []):
            # bps vem da interface de saida real da rota (WAN local ou transito)
            stats = ifaces.get(link.get("out_interface") or wan, {})
            out.append({
                "router": name,
                "label": rsnap.get("label", name),
                "isp": link.get("isp"),
                "route_comment": link.get("route_comment"),
                "gateway": link.get("gateway"),
                "out_interface": link.get("out_interface"),
                "via_transit": link.get("via_transit", False),
                "up": not link.get("disabled", True),
                "active": link.get("active", False),
                "distance": link.get("distance"),
                "rtt_ms": link.get("rtt_ms"),
                "loss_pct": link.get("loss_pct"),
                "rx_bps": stats.get("rx_bps"),
                "tx_bps": stats.get("tx_bps"),
                "reachable": rsnap.get("reachable", False),
            })
    return {"ts": snap.get("ts"), "links": out}


@app.get("/api/hosts/top")
async def hosts_top(window: int = 60, limit: int = 20) -> Dict[str, Any]:
    """Top talkers locais agregados do IPFIX.

    rx_bps = download p/ o host, tx_bps = upload. Nome vem dos leases
    DHCP (host-name > comment > mac) dos dois MKs; "" se desconhecido.
    """
    window = max(10, min(window, 300))
    limit = max(1, min(limit, 100))
    # ip -> nome amigavel via leases do ultimo snapshot do poller
    names: Dict[str, str] = {}
    for rsnap in poller.latest_snapshot.get("routers", {}).values():
        for l in rsnap.get("leases", []):
            addr = l.get("address")
            if addr:
                names[addr] = (l.get("host-name") or l.get("comment")
                               or l.get("mac-address") or "")
    top = [{"ip": t["ip"], "name": names.get(t["ip"], ""),
            "rx_bps": t["rx_bps"], "tx_bps": t["tx_bps"],
            "total_bps": t["total_bps"]}
           for t in ipfix.top_talkers(window_s=window, limit=limit)]
    return {
        "ts": time.time(),
        "window_s": window,
        "exporters": ipfix.stats["by_exporter"],
        "data_records": ipfix.stats["data_records"],
        "top": top,
    }


@app.get("/api/leases")
async def leases() -> Dict[str, Any]:
    snap = poller.latest_snapshot
    out = []
    for name, rsnap in snap.get("routers", {}).items():
        for l in rsnap.get("leases", []):
            out.append({
                "router": name,
                "address": l.get("address"),
                "mac": l.get("mac-address"),
                "host_name": l.get("host-name") or l.get("comment") or "",
                "status": l.get("status"),
                "expires": l.get("expires-after") or l.get("lease-time"),
            })
    return {"ts": snap.get("ts"), "count": len(out), "leases": out}


@app.get("/api/events")
async def events(limit: int = 100) -> Dict[str, Any]:
    return {"events": await db.recent_events(limit=min(limit, 500))}


@app.get("/api/history")
async def history(hours: int = 1) -> Dict[str, Any]:
    """Rollups de 1min por roteador p/ os graficos do dashboard.

    Num fresh boot samples_1m ainda esta vazio — devolve series vazias e
    o frontend mostra "sem dados ainda".
    """
    hours = max(1, min(hours, 168))
    cutoff = int(time.time()) - hours * 3600
    series: Dict[str, Any] = {}
    for name in poller.targets:
        rows = await db.rollups_since(name, cutoff)
        series[name] = [{
            "ts_min": r["ts_min"],
            "cpu_avg": r["metric"].get("cpu_avg"),
            "wan_rx_bps_avg": r["metric"].get("wan_rx_bps_avg"),
            "wan_tx_bps_avg": r["metric"].get("wan_tx_bps_avg"),
            "rtt_ms_avg": r["metric"].get("rtt_ms_avg"),
            # passa adiante link_rx_<i>/link_tx_<i>/link_rtt_<i>/link_up_<i>
            **{k: v for k, v in r["metric"].items()
               if k.startswith("link_")},
        } for r in rows]
    return {"ts": time.time(), "hours": hours, "series": series}


# ------------------------------------------------------------------ devices

@app.get("/api/devices")
async def devices(new_days: int = 7,
                  router: Optional[str] = None) -> Dict[str, Any]:
    """Devices vistos via DHCP leases. is_new = first_seen < new_days dias."""
    new_days = max(1, min(new_days, 90))
    items = await db.list_devices(new_days=new_days)
    if router:
        items = [d for d in items if d["router"] == router]
    return {"ts": time.time(), "new_days": new_days,
            "count": len(items), "devices": items}


@app.get("/api/devices/stats")
async def devices_stats(new_days: int = 7) -> Dict[str, Any]:
    return await db.device_stats(new_days=max(1, min(new_days, 90)))


# ------------------------------------------------------------------ baseline

@app.get("/api/baseline")
async def baseline() -> Dict[str, Any]:
    """Baselines 7d (median/p95/avg) por roteador+metrica.

    Calcula on-demand se a tabela ainda estiver vazia (fresh boot).
    """
    try:
        if not await db.has_baselines():
            n = await db.compute_baselines(list(poller.targets))
            if n:
                log.info("baselines on-demand: %d linhas", n)
    except Exception:
        log.exception("compute_baselines on-demand falhou")
    return {"ts": time.time(), "days": 7,
            "routers": await db.latest_baselines()}


# ------------------------------------------------------------------ monitoring v2

@app.get("/api/uptime")
async def uptime(days: int = 7) -> Dict[str, Any]:
    """Uptime por link = media de link_up_<i> nos rollups do periodo.

    link_up_<i> e escrito no rollup de 1min a partir do links_up do sample
    (1 = rota do link habilitada no ciclo). Minutos sem rollup (roteador
    fora do ar) simplesmente nao entram na media — honesto e simples.
    """
    days = max(1, min(days, 30))
    cutoff = int(time.time()) - days * 86400
    routers: Dict[str, Any] = {}
    for name in poller.targets:
        rows = await db.rollups_since(name, cutoff)
        acc: Dict[str, list] = {}
        for r in rows:
            for k, v in r["metric"].items():
                if k.startswith("link_up_") and v is not None:
                    acc.setdefault(k[8:], []).append(v)
        routers[name] = {
            idx: {"uptime": sum(vs) / len(vs), "samples": len(vs)}
            for idx, vs in sorted(acc.items(), key=lambda kv: int(kv[0]))
        }
    return {"ts": time.time(), "days": days, "routers": routers}


@app.get("/api/healthscore")
async def healthscore() -> Dict[str, Any]:
    """Score 0-100 por link e por roteador (min dos links).

    Penalidades sobre o snapshot atual + baselines 7d:
      -40 rota disabled ou roteador inalcancavel
      -25 rtt > baseline p95; -15 rtt > 2x mediana
      -40 loss > 5%;        -20 loss > 1%
      -15 rx < 10% da mediana de wan_rx com link ativo
    """
    baselines = await db.latest_baselines()
    snap = poller.latest_snapshot
    routers: Dict[str, Any] = {}
    global_score = 100
    for name, rsnap in snap.get("routers", {}).items():
        bl = baselines.get(name, {})
        rtt_bl = bl.get("rtt_ms_avg") or {}
        rx_bl = bl.get("wan_rx_bps_avg") or {}
        links_out = []
        if not rsnap.get("reachable"):
            rscore = max(0, 100 - 40)
        else:
            wan = rsnap.get("wan_interface", "")
            scores = []
            for i, link in enumerate(rsnap.get("links", [])):
                score = 100
                pen = []
                if link.get("disabled"):
                    score -= 40
                    pen.append("disabled")
                rtt = link.get("rtt_ms")
                if rtt is not None:
                    p95 = rtt_bl.get("p95")
                    med = rtt_bl.get("median")
                    if p95 is not None and rtt > p95:
                        score -= 25
                        pen.append("rtt>p95")
                    elif med is not None and rtt > 2 * med:
                        score -= 15
                        pen.append("rtt>2xmedian")
                loss = link.get("loss_pct")
                if loss is not None:
                    if loss > 5:
                        score -= 40
                        pen.append("loss>5%")
                    elif loss > 1:
                        score -= 20
                        pen.append("loss>1%")
                stats = (rsnap.get("interfaces", {})
                         .get(link.get("out_interface") or wan) or {})
                rx = stats.get("rx_bps")
                med_rx = rx_bl.get("median")
                if (link.get("active") and med_rx and rx is not None
                        and rx < 0.1 * med_rx):
                    score -= 15
                    pen.append("rx<10%baseline")
                score = max(0, min(100, int(round(score))))
                links_out.append({"index": i, "isp": link.get("isp"),
                                  "score": score, "penalties": pen})
                scores.append(score)
            rscore = min(scores) if scores else 100
        routers[name] = {"score": rscore, "links": links_out}
        global_score = min(global_score, rscore)
    return {"ts": time.time(), "global": global_score, "routers": routers}


@app.get("/api/heatmap")
async def heatmap(days: int = 7) -> Dict[str, Any]:
    """Matriz dia x hora de wan_rx_bps_avg (media dos rollups no bucket).

    So emite celulas com dados — o frontend pinta o resto como vazio.
    """
    days = max(1, min(days, 30))
    cutoff = int(time.time()) - days * 86400
    routers: Dict[str, Any] = {}
    for name in poller.targets:
        rows = await db.rollups_since(name, cutoff)
        acc: Dict[Any, list] = {}
        for r in rows:
            v = r["metric"].get("wan_rx_bps_avg")
            if v is None:
                continue
            lt = time.localtime(r["ts_min"])
            key = (time.strftime("%Y-%m-%d", lt), lt.tm_hour)
            acc.setdefault(key, []).append(v)
        routers[name] = [
            {"day": d, "hour": h, "v": sum(vs) / len(vs)}
            for (d, h), vs in sorted(acc.items())
        ]
    return {"ts": time.time(), "days": days, "routers": routers}


@app.get("/api/talkers/total")
async def talkers_total(hours: int = 24, router: Optional[str] = None,
                        limit: int = 30) -> Dict[str, Any]:
    """Top consumidores por BYTES totais (nao taxa) no periodo — ip_1m."""
    hours = max(1, min(hours, 168))
    since = int(time.time()) - hours * 3600
    if router and router not in poller.targets:
        router = None
    return {"ts": time.time(), "hours": hours,
            "talkers": await db.ip_totals(since, router, limit=min(limit, 100))}


@app.get("/api/talkers/spark")
async def talkers_spark(ips: str = "", hours: int = 1) -> Dict[str, Any]:
    """Serie {t,rx,tx} por IP (ip_1m) p/ sparklines dos top talkers."""
    hours = max(1, min(hours, 168))
    ip_list = [i.strip() for i in ips.split(",") if i.strip()][:40]
    since = int(time.time()) - hours * 3600
    series = await db.ip_series(ip_list, since)
    return {"ts": time.time(), "hours": hours, "series": series}


# ------------------------------------------------------------------ wifi

@app.get("/api/wifi")
async def wifi() -> Dict[str, Any]:
    """Cache do ultimo poll do Tenda RX2 Pro (poll interno a cada ~15s)."""
    return wifi_poller.latest


@app.get("/api/adguard")
async def adguard() -> Dict[str, Any]:
    """Cache do poller do AdGuard Home (DNS da rede)."""
    return adguard_poller.latest


# ------------------------------------------------------------------ actions

class ProposeBody(BaseModel):
    kind: str
    router: str
    params: Dict[str, Any] = {}
    auto: bool = False


@app.get("/api/actions")
async def list_actions(limit: int = 50,
                       status: Optional[str] = None) -> Dict[str, Any]:
    return {
        "templates": [t.describe() for t in REGISTRY.values()],
        "actions": await db.list_actions(limit=min(limit, 200), status=status),
        "read_only": actions.read_only,
    }


@app.post("/api/actions", status_code=201)
async def propose_action(body: ProposeBody) -> Dict[str, Any]:
    try:
        return await actions.propose(body.kind, body.router,
                                     body.params, auto=body.auto)
    except ParamError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/api/actions/{action_id}/approve")
async def approve_action(action_id: int) -> Dict[str, Any]:
    try:
        return await actions.approve(action_id, approved_by="ui")
    except ParamError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/api/actions/{action_id}/reject")
async def reject_action(action_id: int) -> Dict[str, Any]:
    try:
        return await actions.reject(action_id)
    except ParamError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


# ------------------------------------------------------------------ agent

class KillSwitchBody(BaseModel):
    read_only: bool = True


class AskBody(BaseModel):
    question: str


@app.get("/api/agent/status")
async def agent_status() -> Dict[str, Any]:
    return {
        "read_only": actions.read_only,
        "allowlist": settings.allowlist(),
        "ntfy_enabled": alert_engine.ntfy_enabled,
        "poll_interval": settings.poll_interval,
        **agent_brain.status(),
    }


@app.post("/api/agent/ask")
async def agent_ask(body: AskBody) -> Dict[str, Any]:
    q = body.question.strip()
    if not q:
        raise HTTPException(status_code=422, detail="pergunta vazia")
    return {"answer": await agent_brain.ask(q), "ts": time.time()}


@app.post("/api/agent/killswitch")
async def killswitch(body: KillSwitchBody) -> Dict[str, Any]:
    actions.read_only = body.read_only
    await db.insert_event(
        "warning" if body.read_only else "info", "agent",
        "kill-switch: agente em %s"
        % ("READ-ONLY" if body.read_only else "modo normal"))
    broadcaster.publish("agent", {"read_only": actions.read_only})
    return {"read_only": actions.read_only}


# ------------------------------------------------------------------ extras

@app.get("/api/ipfix/stats")
async def ipfix_stats() -> Dict[str, Any]:
    return ipfix.stats


@app.get("/api/stream")
async def stream() -> StreamingResponse:
    q = broadcaster.subscribe()

    async def gen():
        try:
            # manda o snapshot atual imediatamente ao conectar
            yield format_sse({"event": "snapshot",
                              "data": poller.latest_snapshot})
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield format_sse(msg)
                except asyncio.TimeoutError:
                    yield b": keepalive\n\n"
        finally:
            broadcaster.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache",
                 "X-Accel-Buffering": "no"},
    )


# ------------------------------------------------------------------ frontend
# Serve o build do React (frontend/dist) quando existir — SPA fallback.
FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"

if FRONTEND_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"),
              name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        candidate = FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        # index.html sem cache — o bundle muda de hash a cada build e um
        # index velho carregaria a UI desatualizada (graficos/paineis)
        return FileResponse(
            FRONTEND_DIST / "index.html",
            headers={"Cache-Control": "no-cache"},)
