"""Testes basicos — nao precisam de roteador nem de deps de rede.

Cobre: schema do banco, dedup de alertas e geracao de diff dos templates
de acao (com RosClient fake em memoria).
"""
import asyncio
import json
import os
import socket
import struct
import sys
import tempfile

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.actions import (REGISTRY, ActionEngine,        # noqa: E402
                         AdjustSqmLimit, ForceHostViaLink,
                         ParamError, ToggleRoute)
from app.agent import AgentBrain, _extract_json        # noqa: E402
from app.alerts import AlertEngine                      # noqa: E402
from app.config import Settings                        # noqa: E402
from app.db import Database                            # noqa: E402
from app.ipfix import IpfixCollector                    # noqa: E402
from app.stream import Broadcaster                     # noqa: E402


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ------------------------------------------------------------------ db

def test_db_schema_and_events():
    with tempfile.TemporaryDirectory() as d:
        db = Database(os.path.join(d, "t.db"))
        run(db.init())
        run(db.insert_sample("mk-a", {"system": {"cpu_load": 5}}))
        eid = run(db.insert_event("warning", "test", "msg1"))
        assert eid >= 1
        events = run(db.recent_events())
        assert events[0]["message"] == "msg1"
        aid = run(db.insert_action("toggle_route", "mk-a",
                                   {"comment": "LINK REDEBR",
                                    "disabled": True},
                                   [["/ip/route/set", "x"]]))
        a = run(db.get_action(aid))
        assert a["status"] == "proposed"
        assert a["payload"]["comment"] == "LINK REDEBR"
        run(db.update_action(aid, status="approved", approved_by="test"))
        assert run(db.get_action(aid))["status"] == "approved"
        dropped = run(db.cleanup())
        assert set(dropped) == {"samples", "rollups", "baselines", "ip_1m"}
        run(db.close())


# ------------------------------------------------------------------ alerts

def _snap(links, cpu=10, reachable=True, leases=()):
    return {"routers": {"mk-a": {
        "reachable": reachable,
        "wan_interface": "ether2",
        "interfaces": {"ether2": {"rx_bps": 5000}},
        "links": links,
        "leases": list(leases),
        "system": {"cpu_load": cpu},
    }}}


def test_alert_dedup():
    eng = AlertEngine(ntfy_url="", dedup_minutes=10)
    eng._leases_seeded = True
    link = {"route_comment": "LINK REDEBR", "disabled": True, "isp": "RedeBR"}
    now = 1000.0
    # baseline UP; DOWN precisa de 2 leituras seguidas p/ confirmar
    eng.evaluate(_snap([{**link, "disabled": False}]), now=now)
    eng.evaluate(_snap([link]), now=now + 1)              # pendente, mudo
    first = eng.evaluate(_snap([link]), now=now + 2)      # confirmado
    assert any("LINK REDEBR" in a.message and a.level == "critical"
               for a in first)
    # mesmo flip nao re-alerta dentro da janela (nao ha nova transicao)
    second = eng.evaluate(_snap([link]), now=now + 3)
    assert not any("LINK REDEBR" in a.message for a in second)


def test_alert_link_up_down_transition():
    eng = AlertEngine(ntfy_url="", dedup_minutes=0)  # sem dedup p/ o teste
    eng._leases_seeded = True
    link = {"route_comment": "LINK REDEBR", "disabled": False, "isp": "RedeBR"}
    eng.evaluate(_snap([link]), now=1.0)
    eng.evaluate(_snap([{**link, "disabled": True}]), now=2.0)   # pendente
    down = eng.evaluate(_snap([{**link, "disabled": True}]), now=2.5)
    assert any(a.level == "critical" for a in down)
    eng.evaluate(_snap([link]), now=3.0)                          # pendente
    up = eng.evaluate(_snap([link]), now=3.5)                     # confirmado
    assert any(a.level == "info" and "UP" in a.message for a in up)


def test_alert_link_flap_does_not_notify():
    """Uma leitura divergente sozinha (flap) nao gera alerta de DOWN/UP."""
    eng = AlertEngine(ntfy_url="", dedup_minutes=0)
    eng._leases_seeded = True
    link = {"route_comment": "LINK REDEBR", "disabled": False, "isp": "RedeBR"}
    eng.evaluate(_snap([link]), now=1.0)
    flap = eng.evaluate(_snap([{**link, "disabled": True}]), now=2.0)
    assert not any(a.key.startswith("link-") for a in flap)   # ainda pendente
    back = eng.evaluate(_snap([link]), now=3.0)
    assert not any(a.key.startswith("link-") for a in back)   # pendente morreu


def test_alert_router_unreachable_and_rtt_spike():
    eng = AlertEngine(ntfy_url="", dedup_minutes=0)
    eng._leases_seeded = True
    # queda transiente (<60s ou <3 ciclos) nao alerta
    eng.evaluate(_snap([], reachable=False), now=100.0)
    transient = eng.evaluate(_snap([], reachable=False), now=110.0)
    assert not any(a.key.startswith("router-down") for a in transient)
    # sustentada: >=3 ciclos E >=60s
    eng.evaluate(_snap([], reachable=False), now=140.0)
    alerts = eng.evaluate(_snap([], reachable=False), now=161.0)
    assert any(a.key.startswith("router-down") for a in alerts)

    link = {"route_comment": "LINK REDEBR", "disabled": False,
            "isp": "RedeBR", "rtt_ms": 10.0}
    for i in range(5):  # alimenta baseline
        eng.evaluate(_snap([link]), now=200 + i)
    spike = eng.evaluate(
        _snap([{**link, "rtt_ms": 150.0}]), now=210)
    assert any(a.key.startswith("rtt-spike") for a in spike)


def test_alert_unknown_lease():
    eng = AlertEngine(ntfy_url="", dedup_minutes=0)
    eng._leases_seeded = True
    alerts = eng.evaluate(_snap([], leases=[
        {"address": "10.2.2.150", "mac-address": "AA:BB:CC:DD:EE:FF"},
    ]), now=1.0)
    assert any("DESCONHECIDO" in a.message for a in alerts)


# ------------------------------------------------------------------ actions

def test_dry_run_diffs():
    f = ForceHostViaLink()
    p = f.validate({"host_ip": "10.2.2.150", "link": "ondalivre"})
    diff = f.dry_run(p)
    flat = " ".join(" ".join(s) for s in diff)
    assert "to-ONDALIVRE" in flat
    assert "10.2.2.150" in flat
    assert "mark-routing" in flat

    t = ToggleRoute()
    p = t.validate({"comment": "LINK REDEBR", "disabled": True})
    assert "disabled=yes" in " ".join(" ".join(s) for s in t.dry_run(p))

    s = AdjustSqmLimit()
    p = s.validate({"queue_name": "SQM-REDEBR", "max_limit": "550M/550M"})
    assert "550M/550M" in " ".join(" ".join(sq) for sq in s.dry_run(p))


def test_param_validation_errors():
    with pytest.raises(ParamError):
        ForceHostViaLink().validate({"host_ip": "not-an-ip",
                                     "link": "redebr"})
    with pytest.raises(ParamError):
        ForceHostViaLink().validate({"host_ip": "10.2.2.1",
                                     "link": "link-inexistente"})
    with pytest.raises(ParamError):
        AdjustSqmLimit().validate({"queue_name": "q",
                                   "max_limit": "muito"})
    with pytest.raises(ParamError):
        ToggleRoute().validate({"comment": "X"})  # falta 'disabled'


class FakeClient:
    """Simula o RouterOS p/ testar capture/commands/check/rollback."""

    def __init__(self, rows=None):
        self.rows = rows or {}
        self.sent = []

    async def print_where(self, path, **query):
        key = (path, tuple(sorted(query.items())))
        return list(self.rows.get(key, []))

    async def run_sentence(self, words):
        self.sent.append(words)
        return []


def test_toggle_route_flow_with_fake_router():
    comment = "LINK REDEBR"
    client = FakeClient(rows={
        ("/ip/route", (("comment", comment),)):
            [{".id": "*5", "disabled": "false"}],
    })
    tpl = ToggleRoute()
    p = tpl.validate({"comment": comment, "disabled": True})
    state = run(tpl.capture_state(client, p))
    cmds = tpl.commands(p, state)
    assert cmds == [["/ip/route/set", "=numbers=*5", "=disabled=yes"]]
    rb = run(tpl.rollback(client, p, state))
    assert rb == [["/ip/route/set", "=numbers=*5", "=disabled=no"]]


def test_action_engine_read_only_blocks_writes():
    with tempfile.TemporaryDirectory() as d:
        settings = Settings(agent_read_only=True)
        db = Database(os.path.join(d, "t.db"))
        run(db.init())
        fake = FakeClient(rows={
            ("/ip/route", (("comment", "LINK REDEBR"),)):
                [{".id": "*5", "disabled": "false"}],
        })
        eng = ActionEngine(settings, db, {"mk-a": fake})
        a = run(eng.propose("toggle_route", "mk-a",
                            {"comment": "LINK REDEBR", "disabled": True}))
        assert a["status"] == "proposed"
        a = run(eng.approve(a["id"], approved_by="test"))
        assert a["status"] == "failed"          # kill-switch
        assert "read-only" in a["result"]
        assert fake.sent == []                  # nada foi escrito
        run(db.close())


def test_action_engine_executes_and_logs():
    with tempfile.TemporaryDirectory() as d:
        settings = Settings()
        db = Database(os.path.join(d, "t.db"))
        run(db.init())
        comment = "LINK REDEBR"
        fake = FakeClient(rows={
            ("/ip/route", (("comment", comment),)):
                [{".id": "*5", "disabled": "false"}],
        })
        eng = ActionEngine(settings, db, {"mk-a": fake})
        a = run(eng.propose("toggle_route", "mk-a",
                            {"comment": comment, "disabled": True}))
        a = run(eng.approve(a["id"]))
        # fake nao muta estado -> check pode reprovar e disparar rollback;
        # o importante e que a 1a sentenca escrita e o set correto
        assert a["status"] in ("executed", "rolledback")
        assert fake.sent[0] == ["/ip/route/set", "=numbers=*5",
                                "=disabled=yes"]
        run(db.close())


def test_autonomous_allowlisted_within_bounds_executes():
    """Kind allowlisted + auto + params dentro dos limites -> executa
    direto (sem approve), com approved_by='autonomous'."""
    with tempfile.TemporaryDirectory() as d:
        settings = Settings(action_allowlist="toggle_route")
        db = Database(os.path.join(d, "t.db"))
        run(db.init())
        fake = FakeClient(rows={
            ("/ip/route", (("comment", "LINK REDEBR"),)):
                [{".id": "*5", "disabled": "false"}],
        })
        eng = ActionEngine(settings, db, {"mk-a": fake},
                           alerts=AlertEngine(ntfy_url=""))
        a = run(eng.propose("toggle_route", "mk-a",
                            {"comment": "LINK REDEBR", "disabled": True},
                            auto=True))
        assert a["approved_by"] == "autonomous"
        # fake nao muta -> check pode reprovar; o que conta e que escreveu
        assert a["status"] in ("executed", "rolledback")
        assert fake.sent[0] == ["/ip/route/set", "=numbers=*5",
                                "=disabled=yes"]
        run(db.close())


def test_autonomous_out_of_bounds_falls_back_to_proposal():
    """Allowlisted MAS fora dos limites -> fila de aprovacao + evento."""
    with tempfile.TemporaryDirectory() as d:
        settings = Settings(action_allowlist="toggle_route")
        db = Database(os.path.join(d, "t.db"))
        run(db.init())
        fake = FakeClient()
        eng = ActionEngine(settings, db, {"mk-a": fake})
        a = run(eng.propose("toggle_route", "mk-a",
                            {"comment": "!==TEST VRF", "disabled": True},
                            auto=True))
        assert a["status"] == "proposed"
        assert a["approved_by"] is None
        assert fake.sent == []                        # nada foi escrito
        evs = run(db.recent_events())
        assert any("fora dos limites" in e["message"] for e in evs)
        run(db.close())


def test_non_allowlisted_still_requires_approval():
    """auto=True sem o kind na allowlist -> proposta normal."""
    with tempfile.TemporaryDirectory() as d:
        settings = Settings()                        # allowlist vazia
        db = Database(os.path.join(d, "t.db"))
        run(db.init())
        fake = FakeClient(rows={
            ("/ip/route", (("comment", "LINK REDEBR"),)):
                [{".id": "*5", "disabled": "false"}],
        })
        eng = ActionEngine(settings, db, {"mk-a": fake})
        a = run(eng.propose("toggle_route", "mk-a",
                            {"comment": "LINK REDEBR", "disabled": True},
                            auto=True))
        assert a["status"] == "proposed"
        assert fake.sent == []
        run(db.close())


def test_autonomous_sqm_bounds():
    """adjust_sqm_limit autonomo: nome SQM-(UP|DL)-* e limite 50M..600M."""
    with tempfile.TemporaryDirectory() as d:
        settings = Settings(action_allowlist="adjust_sqm_limit")
        db = Database(os.path.join(d, "t.db"))
        run(db.init())
        fake = FakeClient(rows={
            ("/queue/simple", (("name", "SQM-UP-ONDA"),)):
                [{".id": "*9", "max-limit": "90M/90M"}],
            ("/queue/simple", (("name", "OUTRA-QUEUE"),)):
                [{".id": "*8", "max-limit": "10M/10M"}],
        })
        eng = ActionEngine(settings, db, {"mk-a": fake})
        ok = run(eng.propose("adjust_sqm_limit", "mk-a",
                             {"queue_name": "SQM-UP-ONDA",
                              "max_limit": "95M/95M"}, auto=True))
        assert ok["status"] in ("executed", "rolledback")
        assert fake.sent[0][0] == "/queue/simple/set"
        n_sent = len(fake.sent)
        # limite acima de 600M -> proposta
        a = run(eng.propose("adjust_sqm_limit", "mk-a",
                            {"queue_name": "SQM-UP-ONDA",
                             "max_limit": "700M/700M"}, auto=True))
        assert a["status"] == "proposed"
        # nome fora do padrao SQM -> proposta
        a = run(eng.propose("adjust_sqm_limit", "mk-a",
                            {"queue_name": "OUTRA-QUEUE",
                             "max_limit": "100M/100M"}, auto=True))
        assert a["status"] == "proposed"
        assert len(fake.sent) == n_sent               # nada mais escrito
        run(db.close())


def test_autonomous_force_host_bounds():
    """force_host_via_link autonomo so p/ host dentro da LAN do roteador."""
    with tempfile.TemporaryDirectory() as d:
        settings = Settings(action_allowlist="force_host_via_link")
        db = Database(os.path.join(d, "t.db"))
        run(db.init())
        fake = FakeClient(rows={
            ("/routing/table", (("name", "to-ONDALIVRE"),)): [],
        })
        eng = ActionEngine(settings, db, {"mk-a": fake})
        # 10.2.2.x esta na lan_subnet do mk-a -> autonomo
        a = run(eng.propose("force_host_via_link", "mk-a",
                            {"host_ip": "10.2.2.50",
                             "link": "ondalivre"}, auto=True))
        assert a["status"] in ("executed", "rolledback")
        assert fake.sent                              # comandos rodaram
        n_sent = len(fake.sent)
        # ip fora da LAN -> proposta
        a = run(eng.propose("force_host_via_link", "mk-a",
                            {"host_ip": "8.8.8.8",
                             "link": "ondalivre"}, auto=True))
        assert a["status"] == "proposed"
        assert len(fake.sent) == n_sent
        run(db.close())


def test_ntfy_suppression_events_only(monkeypatch):
    """Alertas notify=False gravam evento mas NAO fazem POST no ntfy;
    notify=True (link DOWN confirmado) posta normalmente."""
    posted = []

    class FakeHttp:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, content=None, headers=None):
            posted.append({"url": url, "content": content,
                           "headers": headers})

    monkeypatch.setattr(httpx, "AsyncClient", FakeHttp)

    with tempfile.TemporaryDirectory() as d:
        db = Database(os.path.join(d, "t.db"))
        run(db.init())
        eng = AlertEngine(ntfy_url="https://ntfy.test/alertas",
                          dedup_minutes=0)
        eng._leases_seeded = True
        # lease desconhecido = events-only: gera alerta, grava, nao posta
        alerts = eng.evaluate(_snap([], leases=[
            {"address": "10.2.2.150", "mac-address": "AA:BB:CC:DD:EE:FF"},
        ]), now=1.0)
        assert alerts
        run(eng.dispatch(alerts, db=db))
        assert posted == []
        evs = run(db.recent_events())
        assert any("DESCONHECIDO" in e["message"] for e in evs)
        # link DOWN confirmado = ntfy-worthy
        link = {"route_comment": "LINK REDEBR", "disabled": False,
                "isp": "RedeBR"}
        eng.evaluate(_snap([link]), now=10.0)
        eng.evaluate(_snap([{**link, "disabled": True}]), now=11.0)
        alerts = eng.evaluate(_snap([{**link, "disabled": True}]), now=12.0)
        run(eng.dispatch(alerts, db=db))
        assert len(posted) == 1
        assert posted[0]["url"] == "https://ntfy.test/alertas"
        assert b"DOWN" in posted[0]["content"]
        # NTFY_ENABLED=false (ntfy_enabled=False) suprime ate critical
        eng2 = AlertEngine(ntfy_url="https://ntfy.test/alertas",
                           ntfy_enabled=False)
        eng2._leases_seeded = True
        eng2.evaluate(_snap([link]), now=20.0)
        eng2.evaluate(_snap([{**link, "disabled": True}]), now=21.0)
        alerts = eng2.evaluate(_snap([{**link, "disabled": True}]), now=22.0)
        run(eng2.dispatch(alerts, db=db))
        assert len(posted) == 1                       # nada novo postado
        run(db.close())


# ------------------------------------------------------------------ ipfix

def _ipfix_packet(sets, domain=0):
    """Monta datagrama IPFIX v10. sets = [(set_id, body), ...]."""
    payload = b"".join(
        struct.pack("!HH", sid, len(body) + 4) + body for sid, body in sets)
    return (struct.pack("!HHIII", 10, 16 + len(payload), 1700000000, 1, domain)
            + payload)


def _template_body(template_id, fields):
    """fields = [(field_id, field_len)]; bit15 do fid = enterprise (+4B)."""
    out = struct.pack("!HH", template_id, len(fields))
    for fid, flen in fields:
        if fid & 0x8000:
            out += struct.pack("!HHI", fid, flen, 29305)   # ent. MikroTik
        else:
            out += struct.pack("!HH", fid, flen)
    return out


_LANS = ["10.2.2.0/24", "10.3.3.0/24", "172.20.20.0/30"]
_ip4 = socket.inet_aton


def _mk_collector():
    return IpfixCollector("0.0.0.0", 0, lan_subnets=_LANS)


def test_ipfix_records_aggregate_local_hosts():
    col = _mk_collector()
    # template: srcIPv4(8) dstIPv4(12) octetDelta(1) proto(4)
    #           + ingressInterface(10) com bit enterprise p/ cobrir o skip
    fields = [(8, 4), (12, 4), (1, 8), (4, 1), (0x8000 | 10, 2)]
    tpl = _template_body(256, fields)

    def rec(src, dst, octets, proto):
        return (_ip4(src) + _ip4(dst) + struct.pack("!Q", octets)
                + bytes([proto]) + b"\x00\x01")

    data = (rec("10.2.2.50", "8.8.8.8", 1500, 6)      # LAN -> WAN: tx
            + rec("1.1.1.1", "10.3.3.20", 3000, 6)   # WAN -> LAN: rx
            + rec("8.8.8.8", "1.1.1.1", 9999, 17))      # WAN <-> WAN: ignora
    col.on_datagram(_ipfix_packet([(2, tpl), (256, data)]),
                    ("10.2.2.1", 40000))

    assert col.stats["packets"] == 1
    assert col.stats["bad_packets"] == 0
    assert col.stats["templates_seen"] == 1
    assert col.stats["data_records"] == 3
    assert col.stats["by_exporter"]["10.2.2.1"]["flows"] == 3

    top = {t["ip"]: t for t in col.top_talkers(window_s=60)}
    assert set(top) == {"10.2.2.50", "10.3.3.20"}
    # src local = upload (tx); dst local = download (rx)
    assert top["10.2.2.50"]["tx_bps"] == pytest.approx(1500 * 8 / 60)
    assert top["10.2.2.50"]["rx_bps"] == 0
    assert top["10.3.3.20"]["rx_bps"] == pytest.approx(3000 * 8 / 60)
    assert top["10.3.3.20"]["tx_bps"] == 0


def test_ipfix_data_before_template_ignored():
    col = _mk_collector()
    data = _ip4("10.2.2.50") + b"\x00" * 20
    col.on_datagram(_ipfix_packet([(300, data)]), ("10.2.2.1", 1))
    assert col.stats["data_records"] == 0
    assert col.stats["bad_packets"] == 0
    assert col.top_talkers() == []


def test_ipfix_top_talkers_window_and_order():
    col = _mk_collector()
    now = 1_000_000.0
    col._bump("10.2.2.10", 6000, 0, now - 30)       # dentro da janela
    col._bump("10.2.2.10", 0, 99999, now - 120)     # fora da janela
    col._bump("10.3.3.5", 12000, 12000, now - 5)
    col._bump("10.2.2.99", 10, 10, now - 400)       # idle >5min -> evict

    top = col.top_talkers(window_s=60, limit=20, now=now)
    assert [t["ip"] for t in top] == ["10.3.3.5", "10.2.2.10"]
    assert top[0]["total_bps"] == pytest.approx(24000 * 8 / 60)
    assert top[1]["rx_bps"] == pytest.approx(6000 * 8 / 60)
    assert top[1]["tx_bps"] == 0     # octetos antigos fora da janela
    # host idle foi removido da agregacao
    assert "10.2.2.99" not in col._buckets
    # limit respeitado
    assert len(col.top_talkers(window_s=60, limit=1, now=now)) == 1


def test_ipfix_lan_to_lan_counts_both_sides():
    col = _mk_collector()
    tpl = _template_body(256, [(8, 4), (12, 4), (1, 8)])
    data = (_ip4("10.2.2.50") + _ip4("10.3.3.20")
            + struct.pack("!Q", 600))
    col.on_datagram(_ipfix_packet([(2, tpl), (256, data)]),
                    ("172.20.20.1", 40000))
    top = {t["ip"]: t for t in col.top_talkers(window_s=60)}
    assert top["10.2.2.50"]["tx_bps"] == pytest.approx(600 * 8 / 60)
    assert top["10.3.3.20"]["rx_bps"] == pytest.approx(600 * 8 / 60)


# ------------------------------------------------------------------ agent

class FakePoller:
    """So o que o AgentBrain consome: latest_snapshot."""

    def __init__(self, snap=None):
        self.latest_snapshot = snap or {"ts": 1, "routers": {}}
        self.clients = {}


def _anthropic_transport(text):
    """MockTransport que finge a Messages API devolvendo `text`."""
    def handler(request):
        assert request.headers["x-api-key"] == "test-key"
        assert request.headers["anthropic-version"] == "2023-06-01"
        body = json.loads(request.content)
        assert body["model"] == "claude-sonnet-4-6"
        assert body["system"]
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": text}]})
    return httpx.MockTransport(handler)


def _mk_brain(d, api_key="test-key", transport=None, snap=None):
    settings = Settings(anthropic_api_key=api_key, llm_backend="anthropic")
    db = Database(os.path.join(d, "t.db"))
    run(db.init())
    actions = ActionEngine(settings, db,
                           {"mk-a": FakeClient(), "mk-b": FakeClient()})
    brain = AgentBrain(settings, db, FakePoller(snap), actions,
                       Broadcaster(), AlertEngine(ntfy_url=""))
    if transport is not None:
        brain._http = httpx.AsyncClient(transport=transport)
    return brain, db


def test_agent_extract_json_contract():
    assert _extract_json('{"insights": []}') == {"insights": []}
    fenced = ('```json\n{"insights": '
              '[{"severity": "info", "text": "ok"}]}\n```')
    assert _extract_json(fenced)["insights"][0]["text"] == "ok"
    prosa = 'analise...\n{"insights": [], "actions": []}\npronto'
    assert _extract_json(prosa)["actions"] == []
    with pytest.raises(ValueError):
        _extract_json("nenhum json aqui")


def test_agent_cycle_insights_and_proposals():
    with tempfile.TemporaryDirectory() as d:
        # resposta fenced p/ cobrir o parse tolerante no caminho inteiro
        payload = "```json\n" + json.dumps({
            "insights": [
                {"severity": "warning", "text": "CPU alta no mk-a"},
                {"severity": "info", "text": "tudo estavel"}],
            "optimizations": ["avaliar subir SQM-UP-ONDA p/ 95M"],
            "actions": [
                {"kind": "toggle_route", "router": "mk-a",
                 "params": {"comment": "LINK ONDA-LIVRE", "disabled": True},
                 "reason": "backup instavel"},
                {"kind": "pcc_balance", "router": "mk-a",
                 "params": {}, "reason": "kind inexistente -> descarte"}],
        }) + "\n```"
        brain, db = _mk_brain(d, transport=_anthropic_transport(payload))
        run(brain._cycle())
        evs = [e for e in run(db.recent_events())
               if e["source"] == "ai-agent"]
        assert any(e["level"] == "warning" and "CPU alta" in e["message"]
                   for e in evs)
        assert any("otimizacao" in e["message"] for e in evs)
        acts = run(db.list_actions())
        assert len(acts) == 1                      # kind ruim descartado
        assert acts[0]["kind"] == "toggle_route"
        assert acts[0]["router"] == "mk-a"
        assert acts[0]["status"] == "proposed"     # fila de aprovacao
        assert acts[0]["payload"]["comment"] == "LINK ONDA-LIVRE"
        assert any("kind desconhecido" in e["message"] for e in evs)
        assert brain.last_run_ts is not None
        run(db.close())


def test_agent_no_api_key_idles():
    with tempfile.TemporaryDirectory() as d:
        # sem transport — se chamasse HTTP seria rede real; nao deve chamar
        brain, db = _mk_brain(d, api_key="")
        run(brain._cycle())
        assert brain._warned_no_key
        assert brain.last_run_ts is None
        assert run(brain.ask("oi?")).startswith("Agente sem LLM configurado")
        run(db.close())


def test_agent_ask_text_and_rate_limit():
    with tempfile.TemporaryDirectory() as d:
        brain, db = _mk_brain(
            d, transport=_anthropic_transport("O link RedeBR esta estavel."))
        ans = run(brain.ask("como esta o link?"))
        assert "RedeBR" in ans
        second = run(brain.ask("e agora?"))
        assert "aguarde" in second.lower()         # rate limit de 10s
        run(db.close())


def test_agent_http_error_propagates_for_backoff():
    with tempfile.TemporaryDirectory() as d:
        def handler(request):
            return httpx.Response(500, json={"error": {"message": "boom"}})
        brain, db = _mk_brain(d, transport=httpx.MockTransport(handler))
        with pytest.raises(httpx.HTTPStatusError):
            run(brain._cycle())   # _run() captura e aplica backoff
        run(db.close())


# ------------------------------------------------------------------ devices

def test_devices_upsert_first_seen_and_is_new():
    """Upsert por MAC preserva first_seen, atualiza ip/last_seen e so
    sobrescreve name quando vem nao-vazio; is_new segue new_days."""
    import time
    with tempfile.TemporaryDirectory() as d:
        db = Database(os.path.join(d, "t.db"))
        run(db.init())
        now = time.time()
        # device novo (visto agora)
        run(db.upsert_devices([
            {"mac": "AA:BB:CC:DD:EE:01", "ip": "10.3.3.50",
             "name": "iphone-rod", "router": "mk-b"},
        ], ts=now))
        # device antigo (first_seen 10d atras) — simulado com ts antigo
        run(db.upsert_devices([
            {"mac": "AA:BB:CC:DD:EE:02", "ip": "10.2.2.20",
             "name": "tv-sala", "router": "mk-a"},
        ], ts=now - 10 * 86400))
        # re-poll do device novo com ip novo e name vazio
        run(db.upsert_devices([
            {"mac": "AA:BB:CC:DD:EE:01", "ip": "10.3.3.51",
             "name": "", "router": "mk-b"},
        ], ts=now + 5))

        devs = {d["mac"]: d for d in run(db.list_devices(new_days=7))}
        novo = devs["AA:BB:CC:DD:EE:01"]
        assert novo["is_new"] is True
        assert novo["ip"] == "10.3.3.51"          # ip atualizado
        assert novo["name"] == "iphone-rod"          # name nao apagado
        assert novo["first_seen"] == pytest.approx(now, abs=1)
        assert novo["last_seen"] == pytest.approx(now + 5, abs=1)
        velho = devs["AA:BB:CC:DD:EE:02"]
        assert velho["is_new"] is False
        # janela larga re-inclui o antigo como "novo"
        devs90 = {d["mac"]: d for d in run(db.list_devices(new_days=30))}
        assert devs90["AA:BB:CC:DD:EE:02"]["is_new"] is True

        stats = run(db.device_stats(new_days=7))
        assert stats["total"] == 2
        assert stats["new"] == 1
        # device antigo so foi visto ha 10d -> nao entra no visto-24h
        assert stats["seen_24h"] == 1
        assert stats["by_router"] == {"mk-a": 1, "mk-b": 1}
        run(db.close())


# ------------------------------------------------------------------ baselines

def test_baseline_math_from_rollups():
    """Seed rollups conhecidos -> median/p95/avg corretos por metrica."""
    import time
    with tempfile.TemporaryDirectory() as d:
        db = Database(os.path.join(d, "t.db"))
        run(db.init())
        now = int(time.time())
        # 10 rollups: rx = 1..10 Mbps (em bps), rtt = 10,20,...
        for i in range(1, 11):
            run(db.insert_rollup("mk-a", {
                "wan_rx_bps_avg": float(i * 1_000_000),
                "wan_tx_bps_avg": float(i * 500_000),
                "rtt_ms_avg": float(i * 10),
                "cpu_avg": float(i),
            }, ts_min=now - (11 - i) * 60))
        n = run(db.compute_baselines(["mk-a", "mk-b"]))
        assert n == 4          # mk-b sem dados -> pula
        latest = run(db.latest_baselines())
        rx = latest["mk-a"]["wan_rx_bps_avg"]
        assert rx["n"] == 10
        assert rx["median"] == pytest.approx(5_500_000)   # (5+6)/2 M
        assert rx["avg"] == pytest.approx(5_500_000)
        assert rx["p95"] == pytest.approx(10_000_000)     # nearest-rank
        rtt = latest["mk-a"]["rtt_ms_avg"]
        assert rtt["median"] == pytest.approx(55)
        assert rtt["p95"] == pytest.approx(100)
        cpu = latest["mk-a"]["cpu_avg"]
        assert cpu["median"] == pytest.approx(5.5)
        run(db.close())


# ------------------------------------------------------------------ history

def test_history_hours_clamp_168():
    """/api/history aceita ate 168h (7d) — clamp de 24 foi ampliado."""
    import app.main as m
    with tempfile.TemporaryDirectory() as d:
        m.db = Database(os.path.join(d, "t.db"))
        run(m.db.init())
        resp = run(m.history(hours=999))
        assert resp["hours"] == 168
        assert set(resp["series"]) == {"mk-a", "mk-b"}
        resp = run(m.history(hours=168))
        assert resp["hours"] == 168
        run(m.db.close())


# ------------------------------------------------------------------ monitoring v2

def test_uptime_avg_math():
    """link_up_<i> dos rollups -> /api/uptime devolve media por indice."""
    import time
    import app.main as m
    with tempfile.TemporaryDirectory() as d:
        m.db = Database(os.path.join(d, "t.db"))
        run(m.db.init())
        now = int(time.time())
        for ts, up0, up1 in ((now - 3600, 1.0, 1.0),
                             (now - 1800, 0.0, 1.0),
                             (now - 60, 1.0, 1.0)):
            run(m.db.insert_rollup("mk-a", {
                "link_up_0": up0, "link_up_1": up1}, ts_min=ts))
        resp = run(m.uptime(days=7))
        up = resp["routers"]["mk-a"]
        assert up["0"]["uptime"] == pytest.approx(2 / 3)
        assert up["0"]["samples"] == 3
        assert up["1"]["uptime"] == pytest.approx(1.0)
        assert resp["routers"]["mk-b"] == {}      # sem rollups -> vazio
        resp = run(m.uptime(days=99))
        assert resp["days"] == 30                 # clamp
        run(m.db.close())


def test_healthscore_penalties():
    """Penalidades: disabled, rtt vs baseline, loss, rx<10%med, unreachable."""
    import time
    import app.main as m
    with tempfile.TemporaryDirectory() as d:
        m.db = Database(os.path.join(d, "t.db"))
        run(m.db.init())
        run(m.db._insert_baseline("mk-a", "rtt_ms_avg", time.time(),
                                  median=10.0, p95=30.0, avg=15.0, n=100))
        run(m.db._insert_baseline("mk-a", "wan_rx_bps_avg", time.time(),
                                  median=1e8, p95=1.5e8, avg=1e8, n=100))
        old_snap = m.poller.latest_snapshot
        try:
            m.poller.latest_snapshot = {"ts": 1, "routers": {
                "mk-a": {
                    "reachable": True, "wan_interface": "ether2",
                    "interfaces": {"ether2": {"rx_bps": 5e6}},
                    "links": [
                        {"isp": "RedeBR", "disabled": False, "active": True,
                         "out_interface": "ether2",
                         "rtt_ms": 40.0, "loss_pct": 6.0},
                        {"isp": "Onda Livre", "disabled": True,
                         "active": False, "out_interface": "ether4",
                         "rtt_ms": None, "loss_pct": None},
                    ]},
                "mk-b": {"reachable": False, "links": [], "interfaces": {}},
            }}
            resp = run(m.healthscore())
        finally:
            m.poller.latest_snapshot = old_snap
        l0 = resp["routers"]["mk-a"]["links"][0]
        # rtt 40 > p95 30 (-25) + loss 6>5 (-40) + rx 5e6 < 10e6 (-15)
        assert l0["score"] == 20
        assert set(l0["penalties"]) == {"rtt>p95", "loss>5%",
                                        "rx<10%baseline"}
        l1 = resp["routers"]["mk-a"]["links"][1]
        assert l1["score"] == 60                  # disabled -> -40
        assert resp["routers"]["mk-a"]["score"] == 20      # min dos links
        assert resp["routers"]["mk-b"]["score"] == 60      # unreachable -40
        assert resp["global"] == 20
        run(m.db.close())


def test_healthscore_healthy_link_is_100():
    """Link ok (rtt <= p95, sem loss, rx normal) -> score 100."""
    import time
    import app.main as m
    with tempfile.TemporaryDirectory() as d:
        m.db = Database(os.path.join(d, "t.db"))
        run(m.db.init())
        run(m.db._insert_baseline("mk-a", "rtt_ms_avg", time.time(),
                                  median=10.0, p95=30.0, avg=15.0, n=100))
        old_snap = m.poller.latest_snapshot
        try:
            m.poller.latest_snapshot = {"ts": 1, "routers": {
                "mk-a": {
                    "reachable": True, "wan_interface": "ether2",
                    "interfaces": {"ether2": {"rx_bps": 5e7}},
                    "links": [{"isp": "RedeBR", "disabled": False,
                               "active": True, "out_interface": "ether2",
                               "rtt_ms": 12.0, "loss_pct": 0.5}]},
            }}
            resp = run(m.healthscore())
        finally:
            m.poller.latest_snapshot = old_snap
        l0 = resp["routers"]["mk-a"]["links"][0]
        assert l0["score"] == 100
        assert l0["penalties"] == []
        assert resp["global"] == 100
        run(m.db.close())


def test_heatmap_shape():
    """/api/heatmap agrega rollups por (dia, hora) — media e so celulas c/ dados."""
    import time
    import app.main as m
    with tempfile.TemporaryDirectory() as d:
        m.db = Database(os.path.join(d, "t.db"))
        run(m.db.init())
        h0 = (int(time.time()) // 3600) * 3600
        # dois rollups na mesma hora (h0-3600) -> media 150; um sem rx -> ignora
        run(m.db.insert_rollup("mk-a", {"wan_rx_bps_avg": 100.0},
                               ts_min=h0 - 3600))
        run(m.db.insert_rollup("mk-a", {"wan_rx_bps_avg": 200.0},
                               ts_min=h0 - 3540))
        run(m.db.insert_rollup("mk-a", {"wan_rx_bps_avg": None},
                               ts_min=h0 - 3480))
        resp = run(m.heatmap(days=7))
        cells = resp["routers"]["mk-a"]
        assert len(cells) == 1
        exp = time.localtime(h0 - 3600)
        assert cells[0]["day"] == time.strftime("%Y-%m-%d", exp)
        assert cells[0]["hour"] == exp.tm_hour
        assert cells[0]["v"] == pytest.approx(150.0)
        run(m.db.close())


def test_ip_1m_insert_and_spark():
    """insert_ip_rollup + /api/talkers/spark -> serie ordenada por t."""
    import time
    import app.main as m
    with tempfile.TemporaryDirectory() as d:
        m.db = Database(os.path.join(d, "t.db"))
        run(m.db.init())
        now = int(time.time()) // 60 * 60
        run(m.db.insert_ip_rollup([
            {"ts_min": now - 120, "router": "mk-a", "ip": "10.2.2.50",
             "rx_bps": 800.0, "tx_bps": 80.0},
            {"ts_min": now - 60, "router": "mk-a", "ip": "10.2.2.50",
             "rx_bps": 1600.0, "tx_bps": 160.0},
            {"ts_min": now - 60, "router": "mk-b", "ip": "10.3.3.20",
             "rx_bps": 400.0, "tx_bps": 40.0},
        ]))
        resp = run(m.talkers_spark(ips="10.2.2.50,10.3.3.20", hours=1))
        s = resp["series"]
        a = s["10.2.2.50"]
        assert [p["t"] for p in a] == sorted(p["t"] for p in a)
        assert [p["rx"] for p in a] == [800.0, 1600.0]
        assert s["10.3.3.20"][0]["rx"] == 400.0
        # ip nao gravado -> serie vazia (mas presente)
        resp2 = run(m.talkers_spark(ips="10.0.0.9", hours=1))
        assert resp2["series"]["10.0.0.9"] == []
        run(m.db.close())
