"""Cliente RouterOS — wrapper async sobre librouteros (que e sincrono).

Padrao: UMA conexao persistente por roteador, serializada por asyncio.Lock,
com cada chamada bloqueante executada via asyncio.to_thread. Reconecta com
backoff exponencial e NUNCA propaga falha de conexao para o poller — retorna
None / dispara RouterUnreachable para o chamador tratar como "router down".

Regras duras respeitadas:
  - nada de torch / queries por conexao (CPU do hEX e fragil);
  - match de objetos sempre por `comment` quando possivel;
  - import do librouteros e LAZY para o modulo carregar sem a dependencia
    (facilita testes sem o pacote instalado).
"""
import asyncio
import logging
import socket
import ssl
import time
from typing import Any, Dict, List, Optional

from .config import RouterTarget, Settings

log = logging.getLogger("netagent.ros")


class RouterUnreachable(Exception):
    """Roteador fora do ar / API indisponivel (tratado como estado, nao erro)."""


def _build_ssl_context(verify: bool) -> "ssl.SSLContext":
    ctx = ssl.create_default_context()
    if not verify:
        # cert auto-assinado gerado pelo setup-netagent-*.rsc
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


class RosClient:
    def __init__(self, target: RouterTarget, settings: Settings) -> None:
        self.target = target
        self.settings = settings
        self._api = None
        self._lock = asyncio.Lock()
        self._connected = False
        self._backoff = 1.0
        self._next_retry = 0.0
        self._last_error = ""

    # ---------- conexao ----------

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_error(self) -> str:
        return self._last_error

    def _connect_sync(self):
        import librouteros  # lazy — modulo carrega sem a dependencia

        kwargs: Dict[str, Any] = dict(
            host=self.target.host,
            username=self.settings.ros_user,
            password=self.settings.ros_pass,
            port=self.settings.ros_port,
            timeout=self.settings.ros_connect_timeout,
        )
        if self.settings.ros_use_ssl:
            # librouteros 4.x: SSL via ssl_wrapper (callable socket->socket)
            ctx = _build_ssl_context(self.settings.ros_ssl_verify)
            host = self.target.host
            kwargs["ssl_wrapper"] = (
                lambda sock: ctx.wrap_socket(sock, server_hostname=host))
        return librouteros.connect(**kwargs)

    def _disconnect_sync(self) -> None:
        if self._api is not None:
            try:
                self._api.close()
            except Exception:
                pass
            self._api = None

    async def _ensure_connected(self) -> None:
        """Garante conexao viva; levanta RouterUnreachable se em backoff/falha."""
        if self._connected and self._api is not None:
            return
        now = time.monotonic()
        if now < self._next_retry:
            raise RouterUnreachable(
                "%s em backoff por mais %.1fs (%s)"
                % (self.target.name, self._next_retry - now, self._last_error))
        try:
            api = await asyncio.to_thread(self._connect_sync)
        except Exception as exc:  # socket, ssl, login — tudo vira unreachable
            self._connected = False
            self._last_error = str(exc)
            self._next_retry = time.monotonic() + self._backoff
            self._backoff = min(self._backoff * 2, 60.0)
            raise RouterUnreachable(
                "%s: %s" % (self.target.name, exc)) from exc
        self._api = api
        self._connected = True
        self._backoff = 1.0
        self._last_error = ""
        log.info("conectado a %s (%s)", self.target.name, self.target.host)

    def _mark_dead(self, exc: Exception) -> None:
        self._connected = False
        self._last_error = str(exc)
        self._disconnect_sync()
        # backoff curto apos queda de conexao viva — falha pode ser transient
        self._next_retry = time.monotonic() + self._backoff
        self._backoff = min(self._backoff * 2, 60.0)

    # ---------- primitiva de comando ----------

    async def cmd(self, path: str, **params: Any) -> List[Dict[str, Any]]:
        """Executa uma sentenca RouterOS (ex.: '/ip/route/print',
        comment='X') e retorna a lista de respostas !re.

        Params Python viram '=k=v' na API (hifens preservados: max_limit ->
        max-limit). Falhas viram RouterUnreachable (conexao) ou sobem como
        TrapError (comando rejeitado pelo RouterOS — erro de logica, propagar).
        """
        words = ["=" + k.replace("_", "-") + "=" + str(v)
                 for k, v in params.items()]

        def _run() -> List[Dict[str, Any]]:
            return [row for row in self._api.rawCmd(path, *words)]

        async with self._lock:
            await self._ensure_connected()
            try:
                return await asyncio.to_thread(_run)
            except Exception as exc:
                if _is_conn_error(exc):
                    self._mark_dead(exc)
                    raise RouterUnreachable(
                        "%s: conexao caiu (%s)" % (self.target.name, exc))
                raise  # TrapError etc.: erro real de comando

    async def print_where(self, path: str, **query: Any) -> List[Dict[str, Any]]:
        """`/path print` com query words '?k=v' (ex.: print_where(
        '/ip/route', comment='LINK REDEBR'))."""
        words = ["?" + k.replace("_", "-") + "=" + str(v)
                 for k, v in query.items()]

        def _run() -> List[Dict[str, Any]]:
            return [row for row in self._api.rawCmd(path + "/print", *words)]

        async with self._lock:
            await self._ensure_connected()
            try:
                return await asyncio.to_thread(_run)
            except Exception as exc:
                if _is_conn_error(exc):
                    self._mark_dead(exc)
                    raise RouterUnreachable(
                        "%s: conexao caiu (%s)" % (self.target.name, exc))
                raise

    # ---------- coletas ----------

    async def get_interfaces(self) -> List[Dict[str, Any]]:
        return await self.cmd("/interface/print")

    async def get_routes(self) -> List[Dict[str, Any]]:
        return await self.cmd("/ip/route/print")

    async def get_dhcp_leases(self) -> List[Dict[str, Any]]:
        return await self.cmd("/ip/dhcp-server/lease/print")

    async def get_system_resource(self) -> Dict[str, Any]:
        rows = await self.cmd("/system/resource/print")
        return rows[0] if rows else {}

    async def get_system_health(self) -> List[Dict[str, Any]]:
        try:
            return await self.cmd("/system/health/print")
        except Exception:
            return []  # hEX pode nao ter sensores — tolera

    async def get_conn_count(self) -> Optional[int]:
        """Total de conexoes no conntrack (count-only — barato p/ CPU)."""
        try:
            rows = await self.cmd("/ip/firewall/connection/print",
                                  count_only="")
            for r in rows:
                if "ret" in r:
                    return int(r["ret"])
        except Exception:
            pass
        return None

    async def ping(self, address: str, count: int = 3) -> Dict[str, Any]:
        """Ping via API (/ping). Retorna {sent, received, rtt_ms avg}.

        Cada reply traz time='12ms' (ou '1s234ms'). Perda total => rtt_ms None.
        """
        rows = await self.cmd("/ping", address=address, count=count)
        rtts = [r for r in rows if "time" in r]
        times_ms = [_parse_ros_time(r["time"]) for r in rtts]
        times_ms = [t for t in times_ms if t is not None]
        last = rows[-1] if rows else {}
        return {
            "address": address,
            "sent": _int(last.get("sent"), count),
            "received": _int(last.get("received"), len(rtts)),
            "rtt_ms": (sum(times_ms) / len(times_ms)) if times_ms else None,
            "loss_pct": None,
        }

    # ---------- escrita (usado por actions.py) ----------

    async def run_sentence(self, words: List[str]) -> List[Dict[str, Any]]:
        """Executa uma sentenca crua: ['/ip/route/set', '=disabled=yes',
        '=numbers=0'] — primeira palavra e o path/comando."""
        if not words:
            return []
        path, args = words[0], words[1:]

        def _run() -> List[Dict[str, Any]]:
            return [row for row in self._api.rawCmd(path, *args)]

        async with self._lock:
            await self._ensure_connected()
            try:
                return await asyncio.to_thread(_run)
            except Exception as exc:
                if _is_conn_error(exc):
                    self._mark_dead(exc)
                    raise RouterUnreachable(
                        "%s: conexao caiu (%s)" % (self.target.name, exc))
                raise


def _is_conn_error(exc: Exception) -> bool:
    """Erros de transporte -> unreachable; TrapError (regra do ROS) propaga."""
    try:
        from librouteros.exceptions import ConnectionClosed, FatalError
        conn_types = (ConnectionClosed, FatalError, socket.error, OSError,
                      ssl.SSLError, TimeoutError, EOFError, BrokenPipeError)
        return isinstance(exc, conn_types)
    except ImportError:
        return isinstance(exc, (socket.error, OSError, ssl.SSLError,
                                TimeoutError, EOFError, BrokenPipeError))


_ROS_TIME_RE = None  # lazy compiled


def _parse_ros_time(value: str) -> Optional[float]:
    """'12ms' -> 12.0 ; '1s234ms' -> 1234.0 ; '500us' -> 0.5 ;
    '1ms179us' -> 1.179 (ROS junta unidades)."""
    global _ROS_TIME_RE
    if _ROS_TIME_RE is None:
        import re
        _ROS_TIME_RE = re.compile(r"(\d+(?:\.\d+)?)(us|ms|s)")
    if not value:
        return None
    mult = {"us": 0.001, "ms": 1.0, "s": 1000.0}
    parts = _ROS_TIME_RE.findall(value)
    if not parts:
        try:
            return float(value)
        except ValueError:
            return None
    return sum(float(n) * mult[u] for n, u in parts)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
