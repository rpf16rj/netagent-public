"""Agente de IA (Fase 3) — analise periodica + Q&A via Claude (Anthropic).

A cada `agent_interval_s` o AgentBrain monta um contexto compacto (snapshot
do poller sem contadores crus, eventos recentes, medias de 1h dos rollups,
top talkers do IPFIX) e chama a Messages API via httpx puro — sem SDK.

A resposta segue um contrato JSON:

  {"insights":[{"severity":"info|warning|critical","text":"..."}],
   "optimizations":["..."],
   "actions":[{"kind":"...","router":"mk-a|mk-b","params":{...},
               "reason":"..."}]}

Insights viram eventos (source "ai-agent") + SSE "agent"; so critical
dispara ntfy reutilizando o caminho do AlertEngine (warning fica so no
dashboard — anti-spam). Actions NUNCA
sao executadas direto: passam por ActionEngine.propose() e caem na fila de
aprovacao (autonomas so se o template nao exigir aprovacao E o kind estiver
na allowlist do .env).

Resiliencia: sem ANTHROPIC_API_KEY o agente loga uma vez e fica idle (loop
continua, pula a chamada); erros de HTTP geram evento + backoff exponencial;
nada aqui pode derrubar o poller nem a app.
"""
import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

import httpx

from .actions import REGISTRY, ActionEngine, ParamError
from .alerts import Alert, AlertEngine
from .config import Settings
from .db import Database
from .ipfix import IpfixCollector
from .poller import Poller
from .stream import Broadcaster

log = logging.getLogger("netagent.agent")

_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"
_HTTP_TIMEOUT_S = 300.0   # LLM local em CPU pode levar minutos por resposta
_MAX_TOKENS_ANALYSIS = 1200
_MAX_TOKENS_ASK = 800
_ASK_MIN_INTERVAL_S = 10.0      # rate limit simples do Q&A
_BACKOFF_MIN_S = 60.0
_BACKOFF_MAX_S = 1800.0
_MAX_LEASES = 30                # cortes p/ manter tokens sob controle
_MAX_EVENTS = 20
_MAX_ITEMS = 10                 # teto de insights/otimizacoes/acoes por ciclo

_LINK_KEYS = ("isp", "route_comment", "gateway", "out_interface",
              "via_transit", "disabled", "active", "distance",
              "rtt_ms", "loss_pct")

# ------------------------------------------------------------------
# prompts — papel + topologia real + regras duras (ver SPEC/README)
# ------------------------------------------------------------------

_SYSTEM_BASE = """Voce e um engenheiro de rede senior cuidando da rede domestica do User.

TOPOLOGIA:
- Dois MikroTik hEX (RB750Gr3, MIPS 880MHz). CPU fragil: chega a ~90%
  single-core quando o SQM satura — nao proponha nada pesado.
- mk-a "hEX" (Casa Mae): LAN 10.2.2.0/24; WAN ether "Link 2 - RedeBR"
  (ISP RedeBR ~600/600 Mbps, gw 10.1.1.254); transito ether4-LAN-ROD
  172.20.20.1/30.
- mk-b "house-b" (Casa User): LAN 10.3.3.0/24; WAN ether1-ONDALIVRE
  (ISP Onda Livre ~400/100 Mbps, gw 10.1.1.1); transito
  ether5-TRANSITO-MAE 172.20.20.2/30.
- Failover: script FAILOVER nos roteadores liga/desliga rotas default por
  COMMENT ("LINK REDEBR" / "LINK ONDA-LIVRE"); rota disabled = link down.
  RedeBR e o primario das duas casas; Onda Livre e o backup. PCC foi
  testado e revertido por consumo de CPU.
- SQM fq_codel nas duas WANs: queues simple SQM-UP-REDEBR (550M) e
  SQM-UP-ONDA (90M), entre outras.

REGRAS DURAS — nunca violar:
- Casar objetos RouterOS SEMPRE por comment, nunca por string de endereco.
- A lista MAE-PERMITIDOS existe nos DOIS roteadores e so pode ser alterada
  nos dois juntos.
- Nunca adicionar cover routes nas tabelas TEST-*.
- Nada de operacao pesada de CPU (PCC, marcacao por conexao que derruba o
  fasttrack) sem aviso explicito do custo.
- Nunca tocar nas regras de isolamento LAN-MAE/LAN-ROD, port-knock nem
  anti-DDoS.
- Toda escrita passa SOMENTE pelo ActionEngine (campo "actions" abaixo):
  voce propoe, um humano aprova."""

_CONTRACT = """
CONTRATO DE SAIDA — responda APENAS com um objeto JSON (sem texto fora):
{"insights":[{"severity":"info|warning|critical","text":"..."}],
 "optimizations":["..."],
 "actions":[{"kind":"toggle_route|force_host_via_link|adjust_sqm_limit",
             "router":"mk-a|mk-b","params":{...},"reason":"..."}]}
Templates e params validos:
- toggle_route: {"comment":"LINK REDEBR|LINK ONDA-LIVRE|...","disabled":true|false}
- force_host_via_link: {"host_ip":"<ipv4 da LAN>","link":"redebr|ondalivre"}
- adjust_sqm_limit: {"queue_name":"<nome da /queue simple>","max_limit":"ex.: 550M/550M"}
Sem achados relevantes? Devolva as tres listas vazias. Maximo 5 por lista."""

_ASK_SUFFIX = """
MODO PERGUNTA: responda em texto corrido (prosa), em portugues, direto e
tecnico. O contrato JSON NAO se aplica aqui."""


# ------------------------------------------------------------------ helpers

def _extract_json(text: str) -> Dict[str, Any]:
    """Parse tolerante: aceita fenced ```json e prosa em volta do objeto."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", t)
        t = re.sub(r"\n?```\s*$", "", t).strip()
    try:
        obj = json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.DOTALL)
        if not m:
            raise ValueError("resposta sem objeto JSON")
        obj = json.loads(m.group(0))
    if not isinstance(obj, dict):
        raise ValueError("JSON nao e um objeto")
    return obj


def _avg_rollups(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Media das medias de 1min da ultima hora -> resumo barato p/ o LLM."""
    out: Dict[str, Any] = {"minutes": len(rows)}
    for k in ("cpu_avg", "wan_rx_bps_avg", "wan_tx_bps_avg", "rtt_ms_avg"):
        vals = [r["metric"].get(k) for r in rows]
        vals = [v for v in vals if v is not None]
        out[k] = round(sum(vals) / len(vals), 1) if vals else None
    return out


# -------------------------------------------------------------------- brain

class AgentBrain:
    """Loop de analise + Q&A sob demanda. Falhas nunca propagam."""

    def __init__(self, settings: Settings, db: Database, poller: Poller,
                 actions: ActionEngine, broadcaster: Broadcaster,
                 alert_engine: Optional[AlertEngine] = None,
                 ipfix: Optional[IpfixCollector] = None) -> None:
        self.settings = settings
        self.db = db
        self.poller = poller
        self.actions = actions
        self.broadcaster = broadcaster
        self.alerts = alert_engine
        self.ipfix = ipfix
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._http: Optional[httpx.AsyncClient] = None
        self._warned_no_key = False
        self._backoff_s = 0.0
        self._last_ask_ts: Optional[float] = None
        self._last_logged_error: Optional[str] = None
        self.last_run_ts: Optional[float] = None
        self.last_error: Optional[str] = None

    def _model(self) -> str:
        return (self.settings.llm_model
                or self.settings.anthropic_model)

    def _llm_ready(self) -> bool:
        """Backend configurado? anthropic exige api_key; openai exige base_url."""
        if self.settings.llm_backend == "anthropic":
            return bool(self.settings.anthropic_api_key)
        return bool(self.settings.llm_base_url)

    def status(self) -> Dict[str, Any]:
        return {
            "agent_enabled": self.settings.agent_enabled,
            "interval_s": self.settings.agent_interval_s,
            "backend": self.settings.llm_backend,
            "model": self._model(),
            "has_api_key": self._llm_ready(),
            "last_run_ts": self.last_run_ts,
            "last_error": self.last_error,
            "backoff_s": self._backoff_s,
        }

    # ---------- ciclo de vida ----------

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="agent")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            wait = float(self.settings.agent_interval_s)
            try:
                await self._cycle()
                self._backoff_s = 0.0
                self._last_logged_error = None
            except Exception as exc:
                self.last_error = str(exc)
                self._backoff_s = min(_BACKOFF_MAX_S,
                                      max(_BACKOFF_MIN_S,
                                          self._backoff_s * 2))
                wait = max(wait, self._backoff_s)
                log.exception("ciclo do agente falhou")
                # evento so na 1a falha de uma sequencia igual (anti-spam)
                if self.last_error != self._last_logged_error:
                    self._last_logged_error = self.last_error
                    try:
                        await self.db.insert_event(
                            "warning", "ai-agent",
                            "ciclo do agente falhou: %s" % exc)
                    except Exception:
                        pass
            try:
                await asyncio.wait_for(self._stop.wait(),
                                       timeout=max(5.0, wait))
            except asyncio.TimeoutError:
                pass

    async def _cycle(self) -> None:
        if not self.settings.agent_enabled:
            return
        if not self._llm_ready():
            if not self._warned_no_key:
                log.info("LLM nao configurado (backend=%s), agente desativado",
                         self.settings.llm_backend)
                self._warned_no_key = True
            return
        await self._analyze()

    # ---------- analise periodica ----------

    async def _analyze(self) -> None:
        ctx = await self._build_context()
        user = ("Avalie o estado atual da rede contra a topologia e as "
                "regras duras. Contexto JSON:\n"
                + json.dumps(ctx, ensure_ascii=False))
        raw = await self._call_llm(_SYSTEM_BASE + _CONTRACT, user,
                                   max_tokens=_MAX_TOKENS_ANALYSIS)
        self.last_run_ts = time.time()
        self.last_error = None
        try:
            result = _extract_json(raw)
        except ValueError as exc:
            self.last_error = "resposta fora do contrato: %s" % exc
            log.warning("resposta do modelo fora do contrato: %s", exc)
            await self.db.insert_event(
                "warning", "ai-agent",
                "resposta do modelo fora do contrato JSON")
            return
        await self._apply_result(result)

    async def _apply_result(self, result: Dict[str, Any]) -> None:
        insights = (result.get("insights") or [])[:_MAX_ITEMS]
        optimizations = (result.get("optimizations") or [])[:_MAX_ITEMS]
        acts = (result.get("actions") or [])[:_MAX_ITEMS]
        now = time.time()

        for ins in insights:
            if not isinstance(ins, dict):
                continue
            sev = str(ins.get("severity", "info")).lower()
            if sev not in ("info", "warning", "critical"):
                sev = "info"
            text = str(ins.get("text", "")).strip()
            if not text:
                continue
            await self.db.insert_event(sev, "ai-agent", text)
            self.broadcaster.publish("agent", {
                "ts": now, "kind": "insight",
                "severity": sev, "text": text})
            if sev == "critical":      # so insight critical vai p/ ntfy
                await self._ntfy(sev, text)

        for opt in optimizations:
            text = str(opt).strip()
            if text:
                await self.db.insert_event(
                    "info", "ai-agent", "otimizacao: " + text)

        for a in acts:
            await self._propose(a)

    async def _ntfy(self, level: str, text: str) -> None:
        """Reusa o caminho ntfy do AlertEngine (POST texto puro)."""
        eng = self.alerts
        if eng is None or not eng.ntfy_enabled:
            return
        await eng._post_ntfy(Alert("ai-agent", level,
                                   "[netagent AI] " + text,
                                   source="ai-agent", notify=True))

    async def _propose(self, item: Any) -> None:
        """Valida e enfileira a acao no ActionEngine — nunca executa direto."""
        if not isinstance(item, dict):
            return
        kind = item.get("kind")
        router = str(item.get("router", ""))
        params = item.get("params") or {}
        reason = str(item.get("reason", "")).strip()
        if kind not in REGISTRY:
            await self.db.insert_event(
                "warning", "ai-agent",
                "acao descartada: kind desconhecido %r" % (kind,))
            return
        # auto=True so vira execucao se o kind estiver na allowlist E os
        # params passarem no check_bounds do template — senao fila normal.
        auto = kind in self.settings.allowlist()
        try:
            action = await self.actions.propose(kind, router, params,
                                                auto=auto)
        except ParamError as exc:
            await self.db.insert_event(
                "warning", "ai-agent",
                "acao %s descartada na validacao: %s" % (kind, exc))
            return
        await self.db.insert_event(
            "info", "ai-agent",
            "acao #%d proposta (%s em %s): %s"
            % (action["id"], kind, router, reason or "sem motivo"))
        self.broadcaster.publish("agent", {
            "ts": time.time(), "kind": "action-proposal",
            "action_id": action["id"], "status": action["status"]})

    # ---------- Q&A sob demanda ----------

    async def ask(self, question: str) -> str:
        """Responde em prosa. Rate-limit simples (1 pergunta/10s)."""
        if not self._llm_ready():
            return ("Agente sem LLM configurado (backend=%s) — "
                    "defina LLM_BASE_URL/LLM_MODEL e reinicie o backend."
                    % self.settings.llm_backend)
        now = time.monotonic()
        delta = (now - self._last_ask_ts
                 if self._last_ask_ts is not None else _ASK_MIN_INTERVAL_S)
        if delta < _ASK_MIN_INTERVAL_S:
            return ("Limite de uma pergunta a cada %ds — aguarde mais %ds."
                    % (_ASK_MIN_INTERVAL_S,
                       round(_ASK_MIN_INTERVAL_S - delta)))
        self._last_ask_ts = now
        ctx = await self._build_context()
        user = ("Pergunta do User: %s\n\nContexto JSON:\n%s"
                % (question.strip(), json.dumps(ctx, ensure_ascii=False)))
        try:
            return await self._call_llm(_SYSTEM_BASE + _ASK_SUFFIX, user,
                                        max_tokens=_MAX_TOKENS_ASK)
        except Exception as exc:
            self.last_error = str(exc)
            log.warning("ask() falhou: %s", exc)
            return "Falha ao consultar o modelo: %s" % exc

    # ---------- contexto ----------

    async def _build_context(self) -> Dict[str, Any]:
        """Contexto compacto p/ o LLM — corta contadores crus e arrays."""
        snap = getattr(self.poller, "latest_snapshot", {}) or {}
        routers: Dict[str, Any] = {}
        names: Dict[str, str] = {}          # ip -> nome (p/ top talkers)
        for name, r in snap.get("routers", {}).items():
            leases = r.get("leases") or []
            for l in leases:
                if l.get("address"):
                    names[l["address"]] = (l.get("host-name")
                                           or l.get("comment") or "")
            routers[name] = {
                "reachable": r.get("reachable", False),
                "label": r.get("label"),
                "isp": r.get("isp"),
                "lan_subnet": r.get("lan_subnet"),
                "wan_interface": r.get("wan_interface"),
                "system": {k: (r.get("system") or {}).get(k)
                           for k in ("cpu_load", "free_memory",
                                     "total_memory", "uptime")},
                # so bps/running — nada de contadores rx_bytes/tx_bytes
                "interfaces": {
                    iname: {"rx_bps": i.get("rx_bps"),
                            "tx_bps": i.get("tx_bps"),
                            "running": i.get("running")}
                    for iname, i in (r.get("interfaces") or {}).items()},
                "links": [{k: l.get(k) for k in _LINK_KEYS}
                          for l in r.get("links", [])],
                "lease_count": len(leases),
                "leases": [{"address": l.get("address"),
                            "host_name": (l.get("host-name")
                                          or l.get("comment") or ""),
                            "status": l.get("status")}
                           for l in leases[:_MAX_LEASES]],
            }
        ctx: Dict[str, Any] = {"ts": time.time(), "routers": routers}

        try:
            ctx["recent_events"] = [
                {"ts": e["ts"], "level": e["level"],
                 "source": e["source"], "message": e["message"]}
                for e in await self.db.recent_events(limit=_MAX_EVENTS)]
        except Exception as exc:
            log.debug("contexto: events indisponiveis: %s", exc)

        try:
            cutoff = int(time.time()) - 3600
            ctx["last_hour_avg"] = {
                name: _avg_rollups(await self.db.rollups_since(name, cutoff))
                for name in routers}
        except Exception as exc:
            log.debug("contexto: rollups indisponiveis: %s", exc)

        if self.ipfix is not None:
            try:
                ctx["top_talkers_5min"] = [
                    {"ip": t["ip"], "name": names.get(t["ip"], ""),
                     "rx_bps": round(t["rx_bps"]),
                     "tx_bps": round(t["tx_bps"])}
                    for t in self.ipfix.top_talkers(window_s=300, limit=10)]
            except Exception as exc:
                log.debug("contexto: ipfix indisponivel: %s", exc)
        return ctx

    # ---------- HTTP ----------

    async def _call_llm(self, system: str, user: str,
                        max_tokens: int = _MAX_TOKENS_ANALYSIS) -> str:
        """Chama o backend configurado (anthropic|openai) via httpx puro."""
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S)
        if self.settings.llm_backend == "openai":
            return await self._call_openai(system, user, max_tokens)
        r = await self._http.post(
            _API_URL,
            headers={"x-api-key": self.settings.anthropic_api_key,
                     "anthropic-version": _API_VERSION,
                     "content-type": "application/json"},
            json={"model": self._model(),
                  "max_tokens": max_tokens,
                  "system": system,
                  "messages": [{"role": "user", "content": user}]})
        r.raise_for_status()
        data = r.json()
        parts = [b.get("text", "") for b in data.get("content", [])
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "".join(parts).strip()

    async def _call_openai(self, system: str, user: str,
                           max_tokens: int) -> str:
        """Endpoint OpenAI-compativel (Ollama /v1, LM Studio, vLLM)."""
        base = self.settings.llm_base_url.rstrip("/")
        headers = {"content-type": "application/json"}
        if self.settings.llm_api_key:
            headers["authorization"] = "Bearer %s" % self.settings.llm_api_key
        r = await self._http.post(
            base + "/chat/completions",
            headers=headers,
            json={"model": self._model(),
                  "max_tokens": max_tokens,
                  "temperature": 0.2,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user}]})
        r.raise_for_status()
        data = r.json()
        choices = data.get("choices") or []
        msg = (choices[0].get("message") or {}) if choices else {}
        return (msg.get("content") or "").strip()
