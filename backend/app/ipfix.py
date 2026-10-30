"""Coletor IPFIX (UDP 2055).

Parse do header IPFIX/NetFlow v9, cache de templates por exportador e
decodificacao dos data records (Fase 2). Octetos dos flows sao agregados
em buckets deslizantes por IP local -> top talkers (rx=download, tx=upload).

Header IPFIX (RFC 7011):  version(2) length(2) export_time(4) seq(4) domain_id(4)
Header NetFlow v9:        version(2) count(2) uptime(4) export_time(4) seq(4) source_id(4)
"""
import asyncio
import ipaddress
import logging
import struct
import time
from typing import Any, Dict, Iterable, Iterator, List, Optional

log = logging.getLogger("netagent.ipfix")

IPFIX_VERSION = 10
NETFLOW_V9 = 9

# Tamanho minimo de um header para sequer tentar parsear
_MIN_LEN = 16

# Field IDs IANA decodificados por record (RFC 7012). Os demais campos do
# template sao pulados pelo tamanho declarado.
F_OCTETS = 1        # octetDeltaCount
# 2=packetDeltaCount  4=protocolIdentifier  7=sourceTransportPort
# 10=ingressInterface  11=destinationTransportPort  14=egressInterface
F_SRC_IP = 8        # sourceIPv4Address (4B big-endian)
F_DST_IP = 12       # destinationIPv4Address (4B big-endian)

# Agregacao top talkers: buckets de _SLOT_S s por IP retidos por _KEEP_S.
_SLOT_S = 10
_KEEP_S = 300
_MAX_IPS = 512      # teto de hosts agregados (defesa de memoria)


class _IpfixProtocol(asyncio.DatagramProtocol):
    def __init__(self, collector: "IpfixCollector") -> None:
        self.collector = collector

    def datagram_received(self, data: bytes, addr) -> None:
        self.collector.on_datagram(data, addr)


class IpfixCollector:
    """Escuta UDP e contabiliza flows. Thread-safe o bastante p/ stats."""

    def __init__(self, host: str, port: int,
                 lan_subnets: Optional[Iterable[str]] = None) -> None:
        self.host = host
        self.port = port
        self._transport: Optional[asyncio.DatagramTransport] = None
        self.stats: Dict[str, Any] = {
            "started_ts": None,
            "packets": 0,
            "bad_packets": 0,
            "by_exporter": {},        # ip do MK -> {"packets": n, "flows": n}
            "templates_seen": 0,
            "data_records": 0,
        }
        # (ip do exportador, domain_id) -> {template_id: [(field_id, len)]}
        self._templates: Dict[Any, Dict[int, list]] = {}
        # sub-redes "locais": flows com src/dst nelas viram rx/tx de host
        self._lan_nets: List[Any] = []
        for s in (lan_subnets or []):
            try:
                self._lan_nets.append(ipaddress.ip_network(str(s).strip()))
            except ValueError:
                log.warning("lan_subnet invalida ignorada: %s", s)
        # ip local -> {slot de _SLOT_S s: [rx_octets, tx_octets]}
        self._buckets: Dict[str, Dict[int, List[int]]] = {}
        self._last_seen: Dict[str, float] = {}

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _IpfixProtocol(self),
            local_addr=(self.host, self.port))
        self._transport = transport  # type: ignore[assignment]
        self.stats["started_ts"] = time.time()
        log.info("IPFIX escutando em udp://%s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    # ---------- parse ----------

    def on_datagram(self, data: bytes, addr,
                    ts: Optional[float] = None) -> None:
        self.stats["packets"] += 1
        exporter = addr[0]
        per = self.stats["by_exporter"].setdefault(
            exporter, {"packets": 0, "flows": 0})
        per["packets"] += 1
        try:
            flows = self._parse(data, exporter, now=ts)
        except Exception as exc:
            self.stats["bad_packets"] += 1
            log.debug("pacote IPFIX invalido de %s: %s", exporter, exc)
            return
        per["flows"] += flows
        self.stats["data_records"] += flows

    def _parse(self, data: bytes, exporter: str,
               now: Optional[float] = None) -> int:
        """Decodifica sets do pacote. Devolve qtde de data records lidos."""
        now = time.time() if now is None else now
        if len(data) < _MIN_LEN:
            raise ValueError("datagrama curto demais")
        version = struct.unpack("!H", data[:2])[0]
        if version == IPFIX_VERSION:
            _, _, _, _, domain = struct.unpack("!HHIII", data[:16])
            offset = 16
        elif version == NETFLOW_V9:
            domain = struct.unpack("!I", data[12:16])[0]
            offset = 20
        else:
            raise ValueError("versao desconhecida: %d" % version)

        # template ids sao por exportador + observation domain
        domain_tpls = self._templates.setdefault((exporter, domain), {})
        data_records = 0
        # varre os sets do pacote (set id 0/2/3 = templates; >255 = data)
        while offset + 4 <= len(data):
            set_id, set_len = struct.unpack("!HH", data[offset:offset + 4])
            if set_len < 4 or offset + set_len > len(data):
                break
            body = data[offset + 4: offset + set_len]
            if set_id in (0, 2):      # NetFlow v9 template / IPFIX template
                self.stats["templates_seen"] += self._parse_templates(
                    body, domain_tpls, ipfix=(set_id == 2))
            elif set_id == 3:         # IPFIX options template (ignora por ora)
                pass
            elif set_id > 255:
                tpl = domain_tpls.get(set_id)
                if tpl:
                    for vals in self._decode_records(body, tpl):
                        self._agg_record(vals, now)
                        data_records += 1
            offset += set_len
        return data_records

    def _parse_templates(self, body: bytes, tpls: Dict[int, list],
                         ipfix: bool = True) -> int:
        """Registra template_id -> [(field_id, field_len)]. Devolve qtd nova."""
        added, off = 0, 0
        while off + 4 <= len(body):
            template_id, field_count = struct.unpack("!HH", body[off:off + 4])
            off += 4
            fields = []
            ok = True
            for _ in range(field_count):
                if off + 4 > len(body):
                    ok = False
                    break
                fid, flen = struct.unpack("!HH", body[off:off + 4])
                off += 4
                if ipfix and fid & 0x8000:
                    # bit15 = enterprise: 4B de enterprise number apos o
                    # par (id, len) — nao entra no tamanho do record
                    off += 4
                fields.append((fid & 0x7FFF, flen))
            if not ok:
                break
            if template_id not in tpls:
                added += 1
            tpls[template_id] = fields
        return added

    @staticmethod
    def _decode_records(body: bytes,
                        tpl: list) -> Iterator[Dict[int, int]]:
        """Decodifica cada record do data set -> {field_id: int} big-endian.

        Templates de tamanho fixo: pula de record em record pelo tamanho.
        Campo com len=0xFFFF e variavel (RFC 7011): 1B de len (<255) ou
        0xFF + 2B. Padding/trailing parcial encerra o loop.
        """
        varlen = any(flen == 0xFFFF for _, flen in tpl)
        rec_len = sum(flen for _, flen in tpl)
        off = 0
        while off < len(body):
            if not varlen and (rec_len <= 0 or off + rec_len > len(body)):
                break
            vals, pos, ok = {}, off, True
            for fid, flen in tpl:
                n = flen
                if flen == 0xFFFF:
                    if pos + 1 > len(body):
                        ok = False
                        break
                    n = body[pos]
                    pos += 1
                    if n == 255:
                        if pos + 2 > len(body):
                            ok = False
                            break
                        n = struct.unpack("!H", body[pos:pos + 2])[0]
                        pos += 2
                if pos + n > len(body):
                    ok = False
                    break
                vals[fid] = int.from_bytes(body[pos:pos + n], "big")
                pos += n
            if not ok or pos == off:
                break
            yield vals
            off = pos

    # ---------- agregacao top talkers ----------

    def _is_local(self, ip: ipaddress.IPv4Address) -> bool:
        return any(ip in net for net in self._lan_nets)

    def _agg_record(self, vals: Dict[int, int], now: float) -> None:
        """Contabiliza octetos do flow: src local -> tx (upload) do host,
        dst local -> rx (download). Flow WAN<->WAN e ignorado."""
        src, dst = vals.get(F_SRC_IP), vals.get(F_DST_IP)
        octets = vals.get(F_OCTETS, 0)
        if src is None or dst is None or octets <= 0:
            return
        try:
            sip = ipaddress.IPv4Address(src)
            dip = ipaddress.IPv4Address(dst)
        except ipaddress.AddressValueError:
            return
        if self._is_local(sip):
            self._bump(str(sip), 0, octets, now)
        if self._is_local(dip):
            self._bump(str(dip), octets, 0, now)

    def _bump(self, ip: str, rx: int, tx: int, ts: float) -> None:
        slots = self._buckets.get(ip)
        if slots is None:
            if len(self._buckets) >= _MAX_IPS:
                oldest = min(self._last_seen, key=self._last_seen.get)
                self._buckets.pop(oldest, None)
                self._last_seen.pop(oldest, None)
            slots = self._buckets[ip] = {}
        min_slot = int((ts - _KEEP_S) // _SLOT_S)
        for s in list(slots):
            if s < min_slot:
                del slots[s]
        cell = slots.setdefault(int(ts // _SLOT_S), [0, 0])
        cell[0] += rx
        cell[1] += tx
        self._last_seen[ip] = max(ts, self._last_seen.get(ip, 0.0))

    def top_talkers(self, window_s: int = 60, limit: int = 20,
                    now: Optional[float] = None) -> List[Dict[str, Any]]:
        """Rates por IP local na janela: rx=download, tx=upload (bps).

        IPs sem flow ha mais de _KEEP_S sao evictados aqui (memoria).
        """
        now = time.time() if now is None else now
        min_slot = int((now - window_s) // _SLOT_S)
        idle_cut = now - _KEEP_S
        out = []
        for ip, slots in list(self._buckets.items()):
            if self._last_seen.get(ip, 0.0) < idle_cut:
                self._buckets.pop(ip, None)
                self._last_seen.pop(ip, None)
                continue
            rx = sum(v[0] for s, v in slots.items() if s >= min_slot)
            tx = sum(v[1] for s, v in slots.items() if s >= min_slot)
            if not rx and not tx:
                continue
            rx_bps = rx * 8.0 / window_s
            tx_bps = tx * 8.0 / window_s
            out.append({"ip": ip, "rx_bps": rx_bps, "tx_bps": tx_bps,
                        "total_bps": rx_bps + tx_bps,
                        "rx_octets": rx, "tx_octets": tx})
        out.sort(key=lambda e: e["total_bps"], reverse=True)
        return out[:max(0, limit)]
