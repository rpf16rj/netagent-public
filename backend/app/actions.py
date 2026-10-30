"""Framework de acoes — controle (futuro) do agente de IA sobre os MKs.

Cada ActionTemplate define:
  - params tipados (validados antes de qualquer coisa);
  - dry_run(): diff = sentencas RouterOS em sintaxe CLI legivel (para o
    humano aprovar; usa "[find comment=X]" como notacao);
  - capture_state(): leitura pre-execucao — resolve comment->.id e guarda
    o estado original para o rollback;
  - commands(): sentencas executadas DE FATO via API (numeros=.id real);
  - check(): pos-verificacao (leitura) — se falhar, roda rollback;
  - rollback(): desfaz a acao via API e devolve as sentencas usadas.

Ciclo de vida:  proposed -> approved -> executed | rolledback | failed
                          \-> rejected

Regras duras: match de objetos RouterOS SEMPRE por comment (a resolucao
comment->.id acontece em capture_state; nunca por endereco), aprovacao
obrigatoria salvo kind presente na allowlist do .env E params dentro dos
limites pre-aprovados do template (check_bounds — fora dos limites cai na
fila de aprovacao mesmo allowlisted), kill-switch global (agent_read_only)
bloqueia TODA escrita. Toda execucao autonoma avisa via ntfy o que fez.
"""
import ipaddress
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from .alerts import Alert, AlertEngine
from .config import RouterTarget, Settings
from .db import Database
from .ros_client import RosClient, RouterUnreachable
from .stream import Broadcaster

log = logging.getLogger("netagent.actions")

LINKS = ("redebr", "ondalivre")
# gateway do link visto de cada roteador (mesma sub-rede 10.1.1.0/24)
LINK_GW = {"redebr": "10.1.1.254", "ondalivre": "10.1.1.1"}
LINK_TABLE = {"redebr": "to-REDEBR", "ondalivre": "to-ONDALIVRE"}


class ParamError(ValueError):
    pass


class Param:
    def __init__(self, name: str, ptype: str, description: str,
                 required: bool = True,
                 choices: Optional[List[str]] = None) -> None:
        self.name = name
        self.ptype = ptype          # str | ipv4 | int | bool | enum
        self.description = description
        self.required = required
        self.choices = choices or []

    def describe(self) -> Dict[str, Any]:
        return {"name": self.name, "type": self.ptype,
                "description": self.description, "required": self.required,
                "choices": self.choices}


def _validate(params_spec: List[Param],
              payload: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for p in params_spec:
        v = payload.get(p.name)
        if v is None:
            if p.required:
                raise ParamError("parametro obrigatorio ausente: %s" % p.name)
            continue
        if p.ptype == "ipv4":
            try:
                v = str(ipaddress.IPv4Address(str(v)))
            except Exception:
                raise ParamError("%s nao e um IPv4 valido: %r" % (p.name, v))
        elif p.ptype == "int":
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise ParamError("%s deve ser int: %r" % (p.name, v))
        elif p.ptype == "bool":
            if isinstance(v, str):
                v = v.strip().lower() in ("1", "true", "yes", "sim")
            v = bool(v)
        elif p.ptype == "enum":
            v = str(v).strip().lower()
            if p.choices and v not in p.choices:
                raise ParamError(
                    "%s deve ser um de %s: %r" % (p.name, p.choices, v))
        else:
            v = str(v)
        out[p.name] = v
    return out


def _ids(rows: List[Dict[str, Any]]) -> str:
    """Junta os .id de linhas do print p/ usar em =numbers=."""
    return ",".join(r[".id"] for r in rows if r.get(".id"))


_RATE_SUFFIX = {"": 1, "k": 1_000, "m": 1_000_000, "g": 1_000_000_000}


def _parse_rate(s: str) -> Optional[int]:
    """'550M' -> 550_000_000 bps; sufixo vazio = bps puro."""
    m = re.match(r"^(\d+)([kKmMgG]?)$", s.strip())
    if not m:
        return None
    return int(m.group(1)) * _RATE_SUFFIX[m.group(2).lower()]


class ActionTemplate:
    """Interface base. Subclasses implementam os hooks."""

    kind = "base"
    description = ""
    params: List[Param] = []
    requires_approval = True      # True: autonomo so dentro do check_bounds

    def validate(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return _validate(self.params, payload)

    def check_bounds(self, params: Dict[str, Any],
                     target: Optional[RouterTarget]) -> Optional[str]:
        """Limites pre-aprovados p/ execucao autonoma (Fase 4).

        Retorna None se os params estao dentro do escopo seguro, ou uma
        string com o motivo — nesse caso a acao cai na fila de aprovacao
        mesmo com o kind presente na allowlist.
        """
        return "template sem limites autonomos definidos"

    def dry_run(self, params: Dict[str, Any]) -> List[List[str]]:
        """Diff legivel (sintaxe CLI) mostrado para aprovacao."""
        raise NotImplementedError

    async def capture_state(self, client: RosClient,
                            params: Dict[str, Any]) -> Dict[str, Any]:
        return {}

    def commands(self, params: Dict[str, Any],
                 state: Dict[str, Any]) -> List[List[str]]:
        return self.dry_run(params)

    async def check(self, client: RosClient, params: Dict[str, Any],
                    state: Dict[str, Any]) -> bool:
        return True

    async def rollback(self, client: RosClient, params: Dict[str, Any],
                       state: Dict[str, Any]) -> List[List[str]]:
        """Desfaz via API. Devolve as sentencas efetivamente executadas."""
        return []

    def describe(self) -> Dict[str, Any]:
        return {"kind": self.kind, "description": self.description,
                "requires_approval": self.requires_approval,
                "params": [p.describe() for p in self.params]}


# ---------------------------------------------------------------- templates

class ForceHostViaLink(ActionTemplate):
    """Marca trafego de um host para sair por um link especifico
    (mangle prerouting + routing-mark -> tabela to-* + rota default nela)."""

    kind = "force_host_via_link"
    description = ("Forca o trafego de um IP da LAN a sair por um link "
                   "especifico via mangle/routing-mark (tabela to-*)")
    params = [
        Param("host_ip", "ipv4", "IP do host na LAN a redirecionar"),
        Param("link", "enum", "Link de saida", choices=list(LINKS)),
    ]

    def check_bounds(self, p: Dict[str, Any],
                     target: Optional[RouterTarget]) -> Optional[str]:
        """Autonomo so p/ host DENTRO da LAN do roteador e link conhecido."""
        if p["link"] not in LINKS:
            return "link desconhecido: %s" % p["link"]
        if target is None or not target.lan_subnet:
            return "roteador sem lan_subnet conhecida"
        try:
            net = ipaddress.IPv4Network(target.lan_subnet, strict=False)
            inside = ipaddress.IPv4Address(p["host_ip"]) in net
        except Exception:
            return "lan_subnet invalida: %s" % target.lan_subnet
        if not inside:
            return ("host_ip %s fora da LAN de %s (%s)"
                    % (p["host_ip"], target.name, target.lan_subnet))
        return None

    def _comment(self, p: Dict[str, Any]) -> str:
        return "netagent force %s via %s" % (p["host_ip"], p["link"])

    def dry_run(self, p: Dict[str, Any]) -> List[List[str]]:
        table, gw, c = LINK_TABLE[p["link"]], LINK_GW[p["link"]], self._comment(p)
        return [
            ["/routing/table/add", "name=" + table, "fib=yes",
             "comment=netagent", "# se a tabela ainda nao existir"],
            ["/ip/route/add", "dst-address=0.0.0.0/0", "gateway=" + gw,
             "routing-table=" + table, "comment=" + c],
            ["/ip/firewall/mangle/add", "chain=prerouting",
             "src-address=" + p["host_ip"], "action=mark-routing",
             "new-routing-mark=" + table, "passthrough=no",
             "comment=" + c],
        ]

    async def capture_state(self, client: RosClient,
                            p: Dict[str, Any]) -> Dict[str, Any]:
        rows = await client.print_where(
            "/routing/table", name=LINK_TABLE[p["link"]])
        return {"table_preexisting": bool(rows)}

    def commands(self, p: Dict[str, Any],
                 state: Dict[str, Any]) -> List[List[str]]:
        table, gw, c = LINK_TABLE[p["link"]], LINK_GW[p["link"]], self._comment(p)
        cmds: List[List[str]] = []
        if not state.get("table_preexisting"):
            cmds.append(["/routing/table/add", "=name=" + table,
                         "=fib=yes", "=comment=netagent"])
        cmds.append(["/ip/route/add", "=dst-address=0.0.0.0/0",
                     "=gateway=" + gw, "=routing-table=" + table,
                     "=comment=" + c])
        cmds.append(["/ip/firewall/mangle/add", "=chain=prerouting",
                     "=src-address=" + p["host_ip"],
                     "=action=mark-routing",
                     "=new-routing-mark=" + table, "=passthrough=no",
                     "=comment=" + c])
        return cmds

    async def check(self, client: RosClient, p: Dict[str, Any],
                    state: Dict[str, Any]) -> bool:
        rows = await client.print_where("/ip/firewall/mangle",
                                        comment=self._comment(p))
        return any(r.get("src-address") == p["host_ip"] for r in rows)

    async def rollback(self, client: RosClient, p: Dict[str, Any],
                       state: Dict[str, Any]) -> List[List[str]]:
        c = self._comment(p)
        done: List[List[str]] = []

        for path in ("/ip/firewall/mangle", "/ip/route"):
            rows = await client.print_where(path, comment=c)
            ids = _ids(rows)
            if ids:
                s = [path + "/remove", "=numbers=" + ids]
                await client.run_sentence(s)
                done.append(s)

        if not state.get("table_preexisting"):
            rows = await client.print_where("/routing/table",
                                            name=LINK_TABLE[p["link"]])
            ids = _ids(rows)
            if ids:
                s = ["/routing/table/remove", "=numbers=" + ids]
                await client.run_sentence(s)
                done.append(s)
        return done


class AdjustSqmLimit(ActionTemplate):
    """Ajusta o max-limit de uma queue simple (SQM/fq_codel)."""

    kind = "adjust_sqm_limit"
    description = "Ajusta o max-limit (up/down) de uma /queue simple"
    params = [
        Param("queue_name", "str", "Nome da queue simple (ex.: SQM-REDEBR)"),
        Param("max_limit", "str",
              "Novo limite up/down, ex.: 550M/550M ou 90M/380M"),
    ]

    _LIMIT_RE = re.compile(r"^\d+[kKmMgG]?(/\d+[kKmMgG]?)?$")
    _SQM_NAME_RE = re.compile(r"^SQM-(UP|DL)-[A-Za-z0-9_.-]+$")
    _SQM_MIN_BPS = 50_000_000        # faixa autonoma aprovada: 50M..600M
    _SQM_MAX_BPS = 600_000_000

    def validate(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = super().validate(payload)
        if not self._LIMIT_RE.match(p["max_limit"]):
            raise ParamError(
                "max_limit invalido (ex.: 550M/550M): %r" % p["max_limit"])
        return p

    def check_bounds(self, p: Dict[str, Any],
                     target: Optional[RouterTarget]) -> Optional[str]:
        """Autonomo so em queue SQM-UP/DL-*, uma por chamada, 50M..600M."""
        if not self._SQM_NAME_RE.match(p["queue_name"]):
            return ("queue fora do padrao SQM-(UP|DL)-* "
                    "(uma por chamada): %s" % p["queue_name"])
        for side in p["max_limit"].split("/"):
            v = _parse_rate(side)
            if v is None or not self._SQM_MIN_BPS <= v <= self._SQM_MAX_BPS:
                return ("max_limit fora da faixa aprovada 50M-600M: %s"
                        % p["max_limit"])
        return None

    def dry_run(self, p: Dict[str, Any]) -> List[List[str]]:
        return [
            ["/queue/simple/set", "[find name=\"%s\"]" % p["queue_name"],
             "max-limit=" + p["max_limit"]],
        ]

    async def capture_state(self, client: RosClient,
                            p: Dict[str, Any]) -> Dict[str, Any]:
        rows = await client.print_where("/queue/simple",
                                        name=p["queue_name"])
        if not rows:
            raise ParamError(
                "queue simple nao encontrada: %s" % p["queue_name"])
        return {"id": rows[0].get(".id", ""),
                "old_max_limit": rows[0].get("max-limit", "")}

    def commands(self, p: Dict[str, Any],
                 state: Dict[str, Any]) -> List[List[str]]:
        return [
            ["/queue/simple/set", "=numbers=" + state["id"],
             "=max-limit=" + p["max_limit"]],
        ]

    async def check(self, client: RosClient, p: Dict[str, Any],
                    state: Dict[str, Any]) -> bool:
        rows = await client.print_where("/queue/simple",
                                        name=p["queue_name"])
        return bool(rows) and rows[0].get("max-limit") == p["max_limit"]

    async def rollback(self, client: RosClient, p: Dict[str, Any],
                       state: Dict[str, Any]) -> List[List[str]]:
        s = ["/queue/simple/set", "=numbers=" + state["id"],
             "=max-limit=" + (state.get("old_max_limit") or "0/0")]
        await client.run_sentence(s)
        return [s]


class ToggleRoute(ActionTemplate):
    """Liga/desliga rota(s) casada(s) pelo COMMENT (mesmo criterio do
    script FAILOVER). disabled=true = link considerado down."""

    kind = "toggle_route"
    description = ("Habilita/desabilita rota casada por comment "
                   "(ex.: 'LINK REDEBR')")
    params = [
        Param("comment", "str", "Comment da rota (ex.: LINK REDEBR)"),
        Param("disabled", "bool", "true = desabilita (link down)"),
    ]

    def check_bounds(self, p: Dict[str, Any],
                     target: Optional[RouterTarget]) -> Optional[str]:
        """Autonomo so p/ rotas 'LINK*' — nunca TEST nem comentario '!=='."""
        c = p["comment"].strip().upper()
        if not c.startswith("LINK") or c.startswith("!==") or "TEST" in c:
            return ("comment %r fora do escopo autonomo "
                    "(so rotas 'LINK*')" % p["comment"])
        return None

    def _flag(self, p: Dict[str, Any]) -> str:
        return "yes" if p["disabled"] else "no"

    def dry_run(self, p: Dict[str, Any]) -> List[List[str]]:
        return [
            ["/ip/route/set", "[find comment=\"%s\"]" % p["comment"],
             "disabled=" + self._flag(p)],
        ]

    async def capture_state(self, client: RosClient,
                            p: Dict[str, Any]) -> Dict[str, Any]:
        rows = await client.print_where("/ip/route", comment=p["comment"])
        if not rows:
            raise ParamError("nenhuma rota com comment=%s" % p["comment"])
        return {"ids": [(r.get(".id", ""), r.get("disabled", "false"))
                        for r in rows],
                "matched": len(rows)}

    def commands(self, p: Dict[str, Any],
                 state: Dict[str, Any]) -> List[List[str]]:
        ids = ",".join(i for i, _ in state.get("ids", []) if i)
        return [["/ip/route/set", "=numbers=" + ids,
                 "=disabled=" + self._flag(p)]]

    async def check(self, client: RosClient, p: Dict[str, Any],
                    state: Dict[str, Any]) -> bool:
        rows = await client.print_where("/ip/route", comment=p["comment"])
        if not rows:
            return False
        want = "true" if p["disabled"] else "false"
        return all(r.get("disabled", "false") == want for r in rows)

    async def rollback(self, client: RosClient, p: Dict[str, Any],
                       state: Dict[str, Any]) -> List[List[str]]:
        done = []
        for rid, orig_disabled in state.get("ids", []):
            if not rid:
                continue
            s = ["/ip/route/set", "=numbers=" + rid,
                 "=disabled=" + ("yes" if orig_disabled == "true" else "no")]
            await client.run_sentence(s)
            done.append(s)
        return done


REGISTRY: Dict[str, ActionTemplate] = {
    t.kind: t for t in (
        ForceHostViaLink(),
        AdjustSqmLimit(),
        ToggleRoute(),
    )
}


# ------------------------------------------------------------------ engine

class ActionEngine:
    """Cria propostas, executa aprovadas, verifica e faz rollback."""

    def __init__(self, settings: Settings, db: Database,
                 clients: Dict[str, RosClient],
                 broadcaster: Optional[Broadcaster] = None,
                 alerts: Optional[AlertEngine] = None) -> None:
        self.settings = settings
        self.db = db
        self.clients = clients
        self.broadcaster = broadcaster
        self.alerts = alerts                  # p/ aviso ntfy de acao autonoma
        self.read_only = settings.agent_read_only   # kill-switch

    # ---- propostas ----

    def _router_target(self, router: str) -> Optional[RouterTarget]:
        for t in self.settings.routers():
            if t.name == router:
                return t
        return None

    def _autonomous_ok(self, tpl: ActionTemplate, kind: str, router: str,
                       params: Dict[str, Any],
                       auto: bool) -> Tuple[bool, Optional[str]]:
        """Autonomo so se: auto=True + kind na allowlist + params dentro
        dos limites pre-aprovados do template. Retorna (ok, motivo)."""
        if not auto or kind not in self.settings.allowlist():
            return False, None
        if not tpl.requires_approval:
            return True, None       # template confiavel: allowlist basta
        reason = tpl.check_bounds(params, self._router_target(router))
        return reason is None, reason

    async def propose(self, kind: str, router: str,
                      payload: Dict[str, Any],
                      auto: bool = False) -> Dict[str, Any]:
        tpl = REGISTRY.get(kind)
        if tpl is None:
            raise ParamError("kind desconhecido: %s" % kind)
        params = tpl.validate(payload)
        if router not in self.clients:
            raise ParamError("router desconhecido: %s" % router)
        diff = tpl.dry_run(params)
        autonomous, reason = self._autonomous_ok(tpl, kind, router,
                                                 params, auto)
        status = "approved" if autonomous else "proposed"
        action_id = await self.db.insert_action(kind, router, params, diff,
                                                status=status)
        if autonomous:
            await self.db.update_action(action_id,
                                        approved_by="autonomous")
        elif reason is not None:
            # allowlisted mas fora dos limites -> vira proposta com motivo
            await self.db.insert_event(
                "info", "actions",
                "acao #%d (%s) fora dos limites autonomos: %s — "
                "fila de aprovacao" % (action_id, kind, reason))
        action = await self.db.get_action(action_id)
        self._publish("action", action)
        if autonomous:
            await self.execute(action_id)
            action = await self.db.get_action(action_id)
            await self._notify_auto(action)
        return action

    async def _notify_auto(self, action: Dict[str, Any]) -> None:
        """Apos QUALQUER execucao autonoma, avisa no ntfy o que o agente
        fez (inclui status final: executed/rolledback/failed)."""
        if self.alerts is None or not self.alerts.ntfy_enabled:
            return
        await self.alerts._post_ntfy(Alert(
            "auto-action:%s" % action["id"], "info",
            "[netagent] acao autonoma executada: %s em %s -> %s (%s)"
            % (action["kind"], action["router"], action["status"],
               action.get("result") or "sem detalhe"),
            source="actions", notify=True))

    # ---- aprovacao ----

    async def approve(self, action_id: int,
                      approved_by: str = "ui") -> Dict[str, Any]:
        action = await self.db.get_action(action_id)
        if action is None:
            raise ParamError("acao %d inexistente" % action_id)
        if action["status"] != "proposed":
            raise ParamError(
                "acao %d nao esta proposed (status=%s)"
                % (action_id, action["status"]))
        await self.db.update_action(action_id, status="approved",
                                    approved_by=approved_by)
        self._publish("action", await self.db.get_action(action_id))
        return await self.execute(action_id)

    async def reject(self, action_id: int) -> Dict[str, Any]:
        action = await self.db.get_action(action_id)
        if action is None:
            raise ParamError("acao %d inexistente" % action_id)
        await self.db.update_action(action_id, status="rejected")
        action = await self.db.get_action(action_id)
        self._publish("action", action)
        return action

    # ---- execucao ----

    async def execute(self, action_id: int) -> Dict[str, Any]:
        action = await self.db.get_action(action_id)
        if action is None:
            raise ParamError("acao %d inexistente" % action_id)
        tpl = REGISTRY[action["kind"]]
        params = action["payload"]

        if self.read_only:
            return await self._finish(
                action_id, "failed",
                "kill-switch ativo: agente em read-only")

        client = self.clients[action["router"]]
        try:
            state = await tpl.capture_state(client, params)
            for sentence in tpl.commands(params, state):
                await client.run_sentence(sentence)
            ok = await tpl.check(client, params, state)
        except RouterUnreachable as exc:
            return await self._finish(action_id, "failed",
                                      "router inalcancavel: %s" % exc)
        except Exception as exc:
            return await self._finish(action_id, "failed",
                                      "erro na execucao: %s" % exc)

        if ok:
            return await self._finish(action_id, "executed",
                                      "check pos-execucao OK")
        # check falhou -> rollback automatico
        try:
            done = await tpl.rollback(client, params, state)
            return await self._finish(action_id, "rolledback",
                                      "check falhou; rollback executado",
                                      rollback=done)
        except Exception as exc:
            return await self._finish(action_id, "failed",
                                      "check falhou E rollback falhou: %s"
                                      % exc)

    async def _finish(self, action_id: int, status: str, result: str,
                      rollback: Optional[List[List[str]]] = None
                      ) -> Dict[str, Any]:
        await self.db.update_action(action_id, status=status,
                                    result=result, rollback=rollback)
        lvl = {"executed": "info", "rolledback": "warning",
               "failed": "critical"}.get(status, "info")
        action = await self.db.get_action(action_id)
        await self.db.insert_event(
            lvl, "actions",
            "acao #%d %s (%s): %s"
            % (action_id, action["kind"], action["router"], result))
        self._publish("action", action)
        return action

    def _publish(self, event: str, data: Any) -> None:
        if self.broadcaster is not None:
            self.broadcaster.publish(event, {"ts": time.time(), "data": data})
