"""Wi-Fi — cliente HTTP do AP Tenda RX2 Pro (10.3.3.178, Casa User).

Fluxo real do webui RX2 (extraido de tenda_poll.py):
  1. POST /login/Auth {"userName":"admin","password":md5_upper(senha)}
     -> resp {"sign","stok"} + Set-Cookie _:USERNAME:_=<hex>
  2. GET /;stok=<stok>/goform/getModules?rand=0.5&modules=<mod> c/ Cookie
  3. resp {"data":"<b64>"} -> AES-CBC(key=sign, iv=EU5H62G9ICGRNI43) -> JSON

Modulos uteis: wifiClientList (clientes), sysStatus, deviceInfo,
wifiBandSteer, wifiFeatureCfg, wifiPower, wifiApScan.
"""
import asyncio
import base64
import hashlib
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

import httpx

log = logging.getLogger("netagent.wifi")

_TIMEOUT_S = 8.0
_IV = b"EU5H62G9ICGRNI43"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TendaConfig"
# nomes reais extraidos do bundle JS do firmware (RE no chunk-common.js)
_MODULES = ("onlineList", "systemStatus", "wifiBasicCfg", "deviceList")
_COOKIE_RE = re.compile(r"_:USERNAME:_=[0-9a-f]+")


def _decrypt_data(sign: str, b64data: str) -> Optional[Any]:
    """AES-CBC(key=sign, iv fixo) sobre o campo `data` base64."""
    try:
        from Crypto.Cipher import AES
        pt = AES.new(sign.encode(), AES.MODE_CBC, _IV).decrypt(
            base64.b64decode(b64data))
        end = pt.rfind(b"}") + 1
        return json.loads(pt[:end].decode("utf-8", "replace"))
    except ImportError:
        log.error("pycryptodome ausente — pip install pycryptodome")
    except Exception as exc:
        log.debug("decrypt data falhou: %s", exc)
    return None


def _pick(d: Dict[str, Any], keys) -> Any:
    low = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        if k in low and low[k] not in (None, ""):
            return low[k]
    return None


def _iter_dict_lists(obj: Any):
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            yield obj
        for x in obj:
            yield from _iter_dict_lists(x)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_dict_lists(v)


def _norm_client(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    mac = _pick(raw, ("mac", "macaddr", "mac_addr"))
    ip = _pick(raw, ("ip", "ipaddr", "ip_addr"))
    if not (mac or ip):
        return None
    rssi = _pick(raw, ("rssi", "signal", "rss", "strength"))
    return {
        "mac": str(mac or ""),
        "ip": str(ip or ""),
        "hostname": str(_pick(raw, ("devname", "hostname", "host_name",
                                    "devicename", "name", "alias")) or ""),
        "band": str(_pick(raw, ("connecttype", "band", "freq", "frequency",
                                "netmode", "wifiname")) or ""),
        "rssi": rssi, "signal": rssi,
        "up_speed": str(_pick(raw, ("upspeed", "uprate")) or ""),
        "down_speed": str(_pick(raw, ("downspeed", "downrate")) or ""),
        "uptime": str(_pick(raw, ("uptime", "connecttime", "onlinetime",
                                  "linktime")) or ""),
    }


class TendaClient:
    """Sessao efemera por poll (stok expira rapido)."""

    def __init__(self, host: str, password: str = "",
                 timeout: float = _TIMEOUT_S) -> None:
        self.host = host
        self.password = password or ""
        self.timeout = timeout
        self.base = host if host.startswith("http") \
            else "http://%s" % host.rstrip("/")

    async def _login(self, client: httpx.AsyncClient
                     ) -> Optional[Dict[str, str]]:
        """POST /login/Auth md5u. O cookie _:USERNAME:_=<hex> vem no
        Set-Cookie da pagina inicial e PRECISA ser reenviado no POST —
        httpx descarta cookie c/ ':' no nome, entao extraio na mao.
        Resposta ok: JSON {"errCode":0,"stok","sign"}."""
        md5u = hashlib.md5(self.password.encode()).hexdigest().upper()
        # Fluxo real (capture devtools): POST /login/Auth c/ XHR+Referer
        # SEM cookie (cookie bobo quebra -> 302). Resposta JSON traz
        # stok+sign e o Set-Cookie _:USERNAME:_=<hex> que identifica a
        # sessao nas chamadas seguintes /;stok=X/goform/getModules.
        try:
            r = await client.post(
                "/login/Auth",
                json={"userName": "admin", "password": md5u},
                headers={"X-Requested-With": "XMLHttpRequest",
                         "Referer": self.base + "/login.html",
                         "Origin": self.base},
                follow_redirects=False)
        except httpx.HTTPError as exc:
            log.debug("tenda login falhou: %s", exc)
            return None
        try:
            j = json.loads(r.text)
            if "stok" in j:
                m2 = _COOKIE_RE.search(
                    str(r.headers.get("set-cookie", "")))
                return {"stok": j["stok"], "sign": j.get("sign", ""),
                        "cookie": m2.group(0) if m2 else ""}
        except ValueError:
            pass
        log.debug("tenda login resp inesperada %s: %.120s",
                  r.status_code, r.text)
        return None

    async def _module(self, client: httpx.AsyncClient, sess: Dict[str, str],
                      name: str) -> Optional[Any]:
        if sess["stok"]:
            path = "/;stok=%s/goform/getModules" % sess["stok"]
        else:
            path = "/goform/getModules"   # sessao por cookie jar
        headers = {"X-Requested-With": "XMLHttpRequest",
                   "Referer": self.base + "/index.html"}
        if sess["cookie"]:
            headers["Cookie"] = sess["cookie"]
        try:
            r = await client.get(path, params={"rand": "0.5",
                                               "modules": name},
                                 headers=headers)
        except httpx.HTTPError as exc:
            log.debug("tenda modulo %s falhou: %s", name, exc)
            return None
        try:
            body = json.loads(r.text)
        except ValueError:
            return None
        if "data" in body:
            if sess["sign"]:
                dec = _decrypt_data(sess["sign"], body["data"])
                log.info("DEBUG modulo %s decrypted: %s", name,
                         json.dumps(dec, ensure_ascii=False)[:800])
                return dec
            return None
        log.info("DEBUG modulo %s raw: %s", name,
                 json.dumps(body, ensure_ascii=False)[:800])
        return body

    async def poll(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"ts": time.time(), "reachable": False,
                               "clients": [], "meta": {}}
        try:
            async with httpx.AsyncClient(
                    base_url=self.base, timeout=self.timeout,
                    follow_redirects=True,
                    headers={"User-Agent": _UA}) as client:
                try:
                    r = await client.get("/")
                    out["reachable"] = r.status_code < 500
                except httpx.HTTPError as exc:
                    out["error"] = "unreachable"
                    out["note"] = "sem resposta http: %s" % type(exc).__name__
                    return out
                if not out["reachable"]:
                    out["error"] = "unreachable"
                    return out
                if not self.password:
                    out["note"] = "senha nao configurada (TENDA_PASS)"
                    return out
                sess = await self._login(client)
                if not sess:
                    out["error"] = "auth"
                    out["note"] = "login falhou — confira TENDA_PASS"
                    return out

                clients: List[Dict[str, Any]] = []
                for mod in _MODULES:
                    payload = await self._module(client, sess, mod)
                    if payload is None:
                        continue
                    if mod in ("onlineList", "deviceList"):
                        for lst in _iter_dict_lists(payload):
                            for d in lst:
                                c = _norm_client(d)
                                if c:
                                    clients.append(c)
                    else:
                        low = {str(k).lower(): v for k, v in payload.items()
                               } if isinstance(payload, dict) else {}
                        for k, v in low.items():
                            kl = k.replace("_", "")
                            if "ssid" in kl or "channel" in kl or \
                                    kl == "chan" or "model" in kl or \
                                    "firmware" in kl:
                                out["meta"].setdefault(k, v)
                out["clients"] = clients
                return out
        except Exception as exc:
            log.exception("poll do tenda falhou")
            out["error"] = "internal"
            out["note"] = str(exc)
            return out


class WifiPoller:
    """Loop async — consulta o Tenda a cada `interval` e guarda o cache."""

    def __init__(self, client: TendaClient,
                 interval: float = 15.0) -> None:
        self.client = client
        self.interval = max(5.0, interval)
        self.latest: Dict[str, Any] = {
            "ts": 0, "reachable": False, "clients": [], "meta": {},
        }
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="wifi")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.latest = await self.client.poll()
            except Exception:
                log.exception("wifi poll falhou")
            try:
                await asyncio.wait_for(self._stop.wait(),
                                       timeout=self.interval)
            except asyncio.TimeoutError:
                pass
