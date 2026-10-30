"""Motor de regras de alerta — avaliado a cada ciclo do poller.

Regras implementadas:
  - link down/up (flip do flag disabled da rota default por comment)
  - link flapping (>=4 transicoes em 10min)
  - WAN ativa sem trafego (rx_bps ~0 por 3 ciclos com rota habilitada)
  - spike de latencia (RTT > 3x baseline OU > 100ms)
  - cpu > 80% sustentada por >= 60s
  - novo DHCP lease (MAC nunca visto); "desconhecido" se sem host-name/comment
  - roteador inalcancavel (>=3 falhas seguidas E >=60s — ignora flap)

Dedup: mesma `key` nao re-alerta dentro de `dedup_minutes` (default 10).
Cada alerta e registrado em `events`; ntfy (POST texto puro) so sai para
regras realmente relevantes — cada Alert declara `notify`:
  - notify=True : link DOWN confirmado / recuperado, router down>60s,
                  cpu>=95%, insight critical do agente
  - notify=False: lease novo/desconhecido, spike de RTT, cpu 80-95%,
                  flapping (1x/hora), WAN sem trafego — so dashboard
  - notify=None : cai no piso global NTFY_MIN_SEVERITY (default warning)
Histerese de link: transicao so e confirmada apos _LINK_CONFIRM_CYCLES
leituras iguais seguidas — flap momentaneo nao notifica.
"""
import logging
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

log = logging.getLogger("netagent.alerts")

_FLAP_WINDOW_S = 600
_FLAP_MIN_TRANSITIONS = 4
_FLAP_DEDUP_S = 3600.0          # flapping: no maximo 1 alerta por hora
_CPU_THRESHOLD = 80.0
_CPU_CRITICAL = 95.0            # >=95% sustentado vira critical + ntfy
_CPU_SUSTAIN_S = 60.0
_RTT_SPIKE_MS = 100.0
_RTT_SPIKE_FACTOR = 3.0
_RTT_BASELINE_N = 40
_ZERO_TRAFFIC_CYCLES = 3
_UNREACHABLE_MIN_S = 60.0       # queda sustentada, nao flap transiente
_UNREACHABLE_MIN_CYCLES = 3     # N falhas seguidas de poll p/ confirmar
_LINK_CONFIRM_CYCLES = 2        # histerese: N leituras p/ confirmar transicao
_LEVEL_RANK = {"info": 1, "warning": 2, "critical": 3}


class Alert:
    def __init__(self, key: str, level: str, message: str,
                 source: str = "alerts",
                 notify: Optional[bool] = None) -> None:
        self.key = key
        self.level = level
        self.source = source
        self.message = message
        # True=forca ntfy, False=so evento, None=decide por min_severity
        self.notify = notify

    def as_dict(self) -> Dict[str, Any]:
        return {"key": self.key, "level": self.level,
                "source": self.source, "message": self.message,
                "notify": self.notify}


class AlertEngine:
    """Stateful: baselines e dedup vivem em memoria (re-semeiam no boot)."""

    def __init__(self, ntfy_url: str = "", dedup_minutes: int = 10,
                 ntfy_enabled: bool = True,
                 min_severity: str = "warning") -> None:
        self.ntfy_url = ntfy_url
        self.dedup_s = dedup_minutes * 60
        self.ntfy_enabled = ntfy_enabled and bool(ntfy_url)
        self._min_rank = _LEVEL_RANK.get(str(min_severity).lower(),
                                         _LEVEL_RANK["warning"])
        self._last_sent: Dict[str, float] = {}
        # estado por regra
        # route_key -> {"confirmed": bool|None, "pending": bool|None, "count": int}
        self._link_state: Dict[str, Dict[str, Any]] = {}
        self._link_transitions: Dict[str, Deque[float]] = {}
        self._rtt_baseline: Dict[str, Deque[float]] = {}
        self._cpu_high_since: Dict[str, float] = {}
        self._known_macs: set = set()
        self._leases_seeded = False
        self._zero_traffic_cycles: Dict[str, int] = {}
        self._unreachable_since: Dict[str, float] = {}
        self._unreachable_cycles: Dict[str, int] = {}
        self._was_unreachable: Dict[str, bool] = {}

    # ---------- dedup ----------

    def _dedup_ok(self, key: str, now: float,
                  dedup_s: Optional[float] = None) -> bool:
        last = self._last_sent.get(key)
        win = self.dedup_s if dedup_s is None else dedup_s
        if last is not None and now - last < win:
            return False
        self._last_sent[key] = now
        return True

    def _should_ntfy(self, a: Alert) -> bool:
        """Flag da regra manda; None cai no piso NTFY_MIN_SEVERITY."""
        if not self.ntfy_enabled:
            return False
        if a.notify is not None:
            return a.notify
        return _LEVEL_RANK.get(a.level, 1) >= self._min_rank

    # ---------- avaliacao ----------

    def evaluate(self, snapshot: Dict[str, Any],
                 now: Optional[float] = None) -> List[Alert]:
        """Recebe o snapshot do ciclo (formato do poller) e devolve alertas."""
        now = now if now is not None else time.time()
        alerts: List[Alert] = []

        for router_name, rsnap in snapshot.get("routers", {}).items():
            self._eval_router(router_name, rsnap, now, alerts)
        return alerts

    def _eval_router(self, name: str, rsnap: Dict[str, Any],
                     now: float, out: List[Alert]) -> None:
        # --- roteador inalcancavel (sustentado, nao flap) ---
        if not rsnap.get("reachable", False):
            self._unreachable_since.setdefault(name, now)
            self._unreachable_cycles[name] = \
                self._unreachable_cycles.get(name, 0) + 1
            self._zero_traffic_cycles.pop(name, None)
            self._was_unreachable[name] = True
            key = "router-down:%s" % name
            if self._unreachable_cycles[name] >= _UNREACHABLE_MIN_CYCLES \
                    and now - self._unreachable_since[name] \
                    >= _UNREACHABLE_MIN_S \
                    and self._dedup_ok(key, now):
                out.append(Alert(
                    key, "critical",
                    "Roteador %s inalcancavel via API ha %ds (%s)"
                    % (name, now - self._unreachable_since[name],
                       rsnap.get("error", "sem resposta")),
                    notify=True))
            return
        # voltou?
        if self._was_unreachable.pop(name, False):
            key = "router-up:%s" % name
            if self._dedup_ok(key, now):
                out.append(Alert(key, "info",
                                 "Roteador %s voltou a responder a API"
                                 % name, notify=True))
        self._unreachable_since.pop(name, None)
        self._unreachable_cycles.pop(name, None)

        self._eval_links(name, rsnap, now, out)
        self._eval_wan_traffic(name, rsnap, now, out)
        self._eval_rtt(name, rsnap, now, out)
        self._eval_cpu(name, rsnap, now, out)
        # leases novos viram evento no poller via devices.first_seen
        # (persistente) — nao dispara mais por MAC visto em memoria

    def _eval_links(self, name: str, rsnap: Dict[str, Any],
                    now: float, out: List[Alert]) -> None:
        """Maquina de estado com histerese por rota (comment).

        So notifica transicoes CONFIRMADAS: DOWN->confirmed-DOWN (critical)
        e confirmed-DOWN->UP (info "recuperado"). Uma leitura divergente
        sozinha vira `pending` — precisa de _LINK_CONFIRM_CYCLES ciclos
        seguidos para virar o estado confirmado (flap nao notifica).
        """
        for link in rsnap.get("links", []):
            rkey = "%s:%s" % (name, link.get("route_comment", "?"))
            disabled = bool(link.get("disabled", False))
            st = self._link_state.setdefault(
                rkey, {"confirmed": None, "pending": None, "count": 0})
            if st["confirmed"] is None:
                # primeira leitura: baseline silenciosa (sem alerta de boot)
                st["confirmed"] = disabled
                continue
            if disabled == st["confirmed"]:
                st["pending"] = None
                st["count"] = 0
                continue
            if st["pending"] == disabled:
                st["count"] += 1
            else:
                st["pending"] = disabled
                st["count"] = 1
            if st["count"] < _LINK_CONFIRM_CYCLES:
                continue  # ainda nao confirmou — pode ser flap
            st["confirmed"] = disabled
            st["pending"] = None
            st["count"] = 0
            trans = self._link_transitions.setdefault(rkey, deque(maxlen=20))
            trans.append(now)
            if disabled:
                key = "link-down:%s" % rkey
                if self._dedup_ok(key, now):
                    out.append(Alert(
                        key, "critical",
                        "Link %s no roteador %s: rota '%s' DOWN"
                        % (link.get("isp", "?"), name,
                           link.get("route_comment", "?")),
                        notify=True))
            else:
                # uma unica nota "recuperado" — o UP nao vira alerta separado
                key = "link-up:%s" % rkey
                if self._dedup_ok(key, now):
                    out.append(Alert(
                        key, "info",
                        "Link %s no roteador %s: rota '%s' RECUPERADA (UP)"
                        % (link.get("isp", "?"), name,
                           link.get("route_comment", "?")),
                        notify=True))
            recent = [t for t in trans if now - t <= _FLAP_WINDOW_S]
            if len(recent) >= _FLAP_MIN_TRANSITIONS:
                fkey = "link-flap:%s" % rkey
                if self._dedup_ok(fkey, now, dedup_s=_FLAP_DEDUP_S):
                    out.append(Alert(
                        fkey, "warning",
                        "Link flapping em %s: rota '%s' mudou %dx em 10min"
                        % (name, link.get("route_comment", "?"),
                           len(recent)),
                        notify=False))

    def _eval_wan_traffic(self, name: str, rsnap: Dict[str, Any],
                          now: float, out: List[Alert]) -> None:
        """Rota default habilitada mas interface WAN sem RX -> suspeito."""
        wan = rsnap.get("wan_interface", "")
        if not wan:
            return
        active = any(not l.get("disabled", True)
                     for l in rsnap.get("links", []))
        rx_bps = (rsnap.get("interfaces", {}).get(wan) or {}).get("rx_bps")
        if rx_bps is None:
            return
        if active and rx_bps < 1.0:
            cycles = self._zero_traffic_cycles.get(name, 0) + 1
            self._zero_traffic_cycles[name] = cycles
            if cycles >= _ZERO_TRAFFIC_CYCLES:
                key = "wan-silent:%s" % name
                if self._dedup_ok(key, now):
                    out.append(Alert(
                        key, "warning",
                        "WAN %s de %s habilitada mas sem trafego RX ha %d ciclos"
                        % (wan, name, cycles), notify=False))
        else:
            self._zero_traffic_cycles[name] = 0

    def _eval_rtt(self, name: str, rsnap: Dict[str, Any],
                  now: float, out: List[Alert]) -> None:
        for link in rsnap.get("links", []):
            rtt = link.get("rtt_ms")
            if rtt is None:
                continue
            lkey = "%s:%s" % (name, link.get("isp", link.get("route_comment")))
            base = self._rtt_baseline.setdefault(
                lkey, deque(maxlen=_RTT_BASELINE_N))
            baseline = (sorted(base)[len(base) // 2] if base else None)
            spike = rtt > _RTT_SPIKE_MS or (
                baseline is not None and rtt > _RTT_SPIKE_FACTOR * baseline)
            if spike:
                key = "rtt-spike:%s" % lkey
                if self._dedup_ok(key, now):
                    out.append(Alert(
                        key, "warning",
                        "Latencia alta no link %s (%s): %.1fms (baseline %s)"
                        % (link.get("isp", "?"), name, rtt,
                           "%.1fms" % baseline if baseline else "n/d"),
                        notify=False))
            else:
                base.append(rtt)

    def _eval_cpu(self, name: str, rsnap: Dict[str, Any],
                  now: float, out: List[Alert]) -> None:
        cpu = (rsnap.get("system") or {}).get("cpu_load")
        if cpu is None:
            return
        if cpu > _CPU_THRESHOLD:
            since = self._cpu_high_since.setdefault(name, now)
            if now - since >= _CPU_SUSTAIN_S:
                # 80-95% so evento; >=95% sustentado e critical -> ntfy
                crit = cpu >= _CPU_CRITICAL
                key = "cpu-%s:%s" % ("crit" if crit else "high", name)
                if self._dedup_ok(key, now):
                    out.append(Alert(
                        key, "critical" if crit else "warning",
                        "CPU de %s em %.0f%% ha mais de %ds"
                        % (name, cpu, now - since), notify=crit))
        else:
            self._cpu_high_since.pop(name, None)

    def _eval_leases(self, name: str, rsnap: Dict[str, Any],
                     now: float, out: List[Alert]) -> None:
        leases = rsnap.get("leases", [])
        macs = {l.get("mac-address") for l in leases if l.get("mac-address")}
        if not self._leases_seeded:
            # primeiro ciclo: baseline silenciosa, nao alerta o parque inteiro
            self._known_macs |= macs
            if len(self._known_macs) == len(macs):
                self._leases_seeded = True
            return
        for l in leases:
            mac = l.get("mac-address")
            if not mac or mac in self._known_macs:
                continue
            self._known_macs.add(mac)
            known = bool(l.get("host-name") or l.get("comment"))
            key = "lease-new:%s" % mac
            if self._dedup_ok(key, now):
                who = l.get("host-name") or l.get("comment") or "desconhecido"
                lvl = "info" if known else "warning"
                out.append(Alert(
                    key, lvl,
                    "Novo DHCP lease em %s: %s (%s)%s"
                    % (name, l.get("address", "?"), who,
                       "" if known else " — DISPOSITIVO DESCONHECIDO"),
                    notify=False))

    # ---------- dispatch ----------

    async def dispatch(self, alerts: List[Alert], db=None,
                       broadcaster=None) -> List[Dict[str, Any]]:
        """ntfy (seletivo) + events + SSE para cada alerta."""
        sent = []
        for a in alerts:
            d = a.as_dict()
            if self._should_ntfy(a):
                await self._post_ntfy(a)
            if db is not None:
                d["event_id"] = await db.insert_event(
                    a.level, a.source, a.message)
            if broadcaster is not None:
                broadcaster.publish("alert", d)
            sent.append(d)
        return sent

    async def _post_ntfy(self, alert: Alert) -> None:
        import httpx  # lazy

        try:
            async with httpx.AsyncClient(timeout=5.0) as cli:
                await cli.post(
                    self.ntfy_url,
                    content=alert.message.encode("utf-8"),
                    headers={
                        "Title": "netagent: %s" % alert.level.upper(),
                        "Priority": {"critical": "5", "warning": "4"}.get(
                            alert.level, "3"),
                        "Tags": "rotating_light" if alert.level == "critical"
                                else "warning",
                    },
                )
        except Exception as exc:
            log.warning("ntfy falhou (%s) — alerta perdido: %s",
                        exc, alert.message)
