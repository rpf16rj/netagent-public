"""Camada de persistencia — SQLite stdlib + asyncio.to_thread.

Schema:
  samples     (ts, router, metric)         — snapshots crus por ciclo de poll
  samples_1m  (ts_min, router, metric)     — rollup de 1 minuto (retencao 30d)
  events      (ts, level, source, message) — alertas e ocorrencias
  actions     (id, ts, status, kind, payload, approved_by, result, rollback)
  devices     (mac, ip, name, router, first_seen, last_seen) — hosts vistos
  baselines   (router, metric, computed_ts, median, p95, avg, n)
  ip_1m       (ts_min, router, ip, rx_bps, tx_bps) — rates IPFIX por host (7d)

WAL mode p/ leitura concorrente do dashboard enquanto o poller escreve.
"""
import json
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

# metricas dos rollups de 1min usadas nos baselines de 7 dias
_BASELINE_METRICS = ("wan_rx_bps_avg", "wan_tx_bps_avg",
                     "rtt_ms_avg", "cpu_avg")

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS samples (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    router  TEXT NOT NULL,
    metric  TEXT NOT NULL          -- JSON do snapshot do ciclo
);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
CREATE INDEX IF NOT EXISTS idx_samples_router_ts ON samples(router, ts);

CREATE TABLE IF NOT EXISTS samples_1m (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_min  INTEGER NOT NULL,      -- epoch truncado ao minuto
    router  TEXT NOT NULL,
    metric  TEXT NOT NULL          -- JSON agregado (medias do minuto)
);
CREATE INDEX IF NOT EXISTS idx_rollup_ts ON samples_1m(ts_min);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    level   TEXT NOT NULL,         -- info | warning | critical
    source  TEXT NOT NULL,         -- alerts | poller | actions | agent | ipfix
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    status      TEXT NOT NULL,     -- proposed|approved|executed|rolledback|failed|rejected
    kind        TEXT NOT NULL,     -- ex.: force_host_via_link
    router      TEXT NOT NULL DEFAULT '',
    payload     TEXT NOT NULL,     -- JSON dos params
    diff        TEXT NOT NULL DEFAULT '',   -- JSON: lista de sentencas RouterOS
    approved_by TEXT,
    result      TEXT,
    rollback    TEXT               -- JSON: sentencas de rollback executadas/pendentes
);

CREATE TABLE IF NOT EXISTS devices (
    mac        TEXT PRIMARY KEY,   -- identidade do dispositivo
    ip         TEXT NOT NULL DEFAULT '',
    name       TEXT NOT NULL DEFAULT '',   -- host-name/comment do lease DHCP
    router     TEXT NOT NULL DEFAULT '',   -- ultimo MK que viu o device
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_devices_last ON devices(last_seen);

CREATE TABLE IF NOT EXISTS baselines (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    router      TEXT NOT NULL,
    metric      TEXT NOT NULL,     -- wan_rx_bps_avg|wan_tx_bps_avg|rtt_ms_avg|cpu_avg
    computed_ts REAL NOT NULL,
    median      REAL,
    p95         REAL,
    avg         REAL,
    n           INTEGER NOT NULL DEFAULT 0   -- amostras de samples_1m usadas
);
CREATE INDEX IF NOT EXISTS idx_baselines_key ON baselines(router, metric, computed_ts);

CREATE TABLE IF NOT EXISTS ip_1m (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_min  INTEGER NOT NULL,      -- epoch truncado ao minuto
    router  TEXT NOT NULL,         -- MK dono da LAN do host (por lan_subnet)
    ip      TEXT NOT NULL,
    rx_bps  REAL,                  -- download do host na janela de 1min
    tx_bps  REAL                   -- upload do host na janela de 1min
);
CREATE INDEX IF NOT EXISTS idx_ip1m_ip_ts ON ip_1m(ip, ts_min);

-- Resultados de speedtest agendado/manual (LXC -> internet via link ativo)
CREATE TABLE IF NOT EXISTS speedtests (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    dl_bps    REAL,               -- download medido (bps)
    ul_bps    REAL,               -- upload medido (bps)
    ping_ms   REAL,
    server    TEXT,               -- servidor usado
    trigger   TEXT NOT NULL       -- 'auto' | 'manual'
);
CREATE INDEX IF NOT EXISTS idx_speedtests_ts ON speedtests(ts);
"""


class Database:
    """Wrapper sqlite3 thread-safe (uma conexao, lock interno).

    Todos os metodos publicos sao async e rodam o sqlite via to_thread
    para nao bloquear o event loop do FastAPI/poller.
    """

    def __init__(self, path: str, sample_retention_hours: int = 24,
                 rollup_retention_days: int = 30,
                 ip_retention_days: int = 7) -> None:
        self.path = path
        self.sample_retention_hours = sample_retention_hours
        self.rollup_retention_days = rollup_retention_days
        self.ip_retention_days = ip_retention_days
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    # ---------- ciclo de vida ----------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        return conn

    async def init(self) -> None:
        import asyncio
        self._conn = await asyncio.to_thread(self._connect)

    async def close(self) -> None:
        import asyncio
        if self._conn is not None:
            await asyncio.to_thread(self._conn.close)
            self._conn = None

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        assert self._conn is not None, "Database.init() nao foi chamado"
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
        assert self._conn is not None, "Database.init() nao foi chamado"
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ---------- samples ----------

    async def insert_sample(self, router: str, metric: Dict[str, Any],
                            ts: Optional[float] = None) -> None:
        import asyncio
        await asyncio.to_thread(
            self._execute,
            "INSERT INTO samples(ts, router, metric) VALUES (?,?,?)",
            (ts or time.time(), router, json.dumps(metric)),
        )

    async def insert_rollup(self, router: str, metric: Dict[str, Any],
                            ts_min: int) -> None:
        import asyncio
        await asyncio.to_thread(
            self._execute,
            "INSERT INTO samples_1m(ts_min, router, metric) VALUES (?,?,?)",
            (ts_min, router, json.dumps(metric)),
        )

    async def insert_speedtest(self, dl_bps: Optional[float],
                               ul_bps: Optional[float],
                               ping_ms: Optional[float], server: str,
                               trigger: str, ts: float) -> None:
        import asyncio
        await asyncio.to_thread(
            self._execute,
            "INSERT INTO speedtests(ts, dl_bps, ul_bps, ping_ms, server,"
            " trigger) VALUES (?,?,?,?,?,?)",
            (ts, dl_bps, ul_bps, ping_ms, server, trigger),
        )

    async def latest_speedtests(self, limit: int = 10
                                ) -> List[Dict[str, Any]]:
        import asyncio
        rows = await asyncio.to_thread(
            self._query,
            "SELECT * FROM speedtests ORDER BY ts DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    async def last_rollup_ts(self, router: str) -> Optional[int]:
        import asyncio
        rows = await asyncio.to_thread(
            self._query,
            "SELECT MAX(ts_min) AS m FROM samples_1m WHERE router=?",
            (router,),
        )
        return rows[0]["m"] if rows and rows[0]["m"] is not None else None

    async def samples_since(self, router: str, ts: float) -> List[Dict[str, Any]]:
        import asyncio
        rows = await asyncio.to_thread(
            self._query,
            "SELECT ts, metric FROM samples WHERE router=? AND ts>=? ORDER BY ts",
            (router, ts),
        )
        return [{"ts": r["ts"], "metric": json.loads(r["metric"])} for r in rows]

    async def rollups_since(self, router: str,
                            ts_min: int) -> List[Dict[str, Any]]:
        """Rollups de 1 min a partir de ts_min (usado p/ contexto do agente)."""
        import asyncio
        rows = await asyncio.to_thread(
            self._query,
            "SELECT ts_min, metric FROM samples_1m "
            "WHERE router=? AND ts_min>=? ORDER BY ts_min",
            (router, ts_min),
        )
        return [{"ts_min": r["ts_min"],
                 "metric": json.loads(r["metric"])} for r in rows]

    # ---------- ip_1m (sparklines por host — Monitoring v2) ----------

    async def insert_ip_rollup(self, rows: List[Dict[str, Any]]) -> None:
        """Batch insert de rates por IP: {ts_min, router, ip, rx_bps, tx_bps}."""
        if not rows:
            return
        import asyncio

        def _run() -> None:
            assert self._conn is not None
            with self._lock:
                self._conn.executemany(
                    "INSERT INTO ip_1m(ts_min, router, ip, rx_bps, tx_bps) "
                    "VALUES (?,?,?,?,?)",
                    [(r["ts_min"], r["router"], r["ip"],
                      r["rx_bps"], r["tx_bps"]) for r in rows])
                self._conn.commit()

        await asyncio.to_thread(_run)

    async def ip_series(self, ips: List[str],
                        since: int) -> Dict[str, List[Dict[str, Any]]]:
        """Serie {t,rx,tx} por IP a partir de `since` (ts_min), ordem por t.

        Junta linhas de roteadores diferentes no mesmo bucket por soma —
        um IP so deveria existir numa LAN, entao na pratica e 1:1.
        """
        import asyncio
        if not ips:
            return {}
        marks = ",".join("?" * len(ips))
        rows = await asyncio.to_thread(
            self._query,
            "SELECT ip, ts_min, SUM(rx_bps) AS rx, SUM(tx_bps) AS tx "
            "FROM ip_1m WHERE ip IN (%s) AND ts_min>=? "
            "GROUP BY ip, ts_min ORDER BY ts_min" % marks,
            tuple(ips) + (since,),
        )
        out: Dict[str, List[Dict[str, Any]]] = {ip: [] for ip in ips}
        for r in rows:
            out.setdefault(r["ip"], []).append(
                {"t": r["ts_min"], "rx": r["rx"], "tx": r["tx"]})
        return out

    async def ip_totals(self, since: int, router: Optional[str] = None,
                        limit: int = 30) -> List[Dict[str, Any]]:
        """Total de bytes por IP no periodo (sum(rate*60s) por bucket).

        rx_bps/tx_bps sao medias do minuto — bytes ~= bps*60. Retorna
        top `limit` por consumo total (rx+tx).
        """
        import asyncio
        where = "ts_min>=?" + (" AND router=?" if router else "")
        params: tuple = (since, router) if router else (since,)
        rows = await asyncio.to_thread(
            self._query,
            "SELECT ip, router, SUM(rx_bps)*60 AS rx_bytes, "
            "SUM(tx_bps)*60 AS tx_bytes, COUNT(*) AS mins "
            "FROM ip_1m WHERE %s GROUP BY ip "
            "ORDER BY (rx_bytes+tx_bytes) DESC LIMIT ?" % where,
            params + (limit,),
        )
        return [{"ip": r["ip"], "router": r["router"],
                 "rx_bytes": r["rx_bytes"], "tx_bytes": r["tx_bytes"],
                 "mins": r["mins"]} for r in rows]

    # ---------- devices ----------

    async def upsert_devices(self, devices: List[Dict[str, Any]],
                             ts: Optional[float] = None) -> set:
        """Upsert por MAC a partir dos leases DHCP de um ciclo de poll.

        first_seen so e gravado na 1a vez que o MAC aparece; name so e
        sobrescrito quando vem nao-vazio (lease pode perder o host-name).
        Retorna o set de MACs inseridos agora (dispositivo novo de verdade
        — persistente em banco, sobrevive a restart do backend).
        """
        now = ts or time.time()
        new_macs = set()

        def _run() -> None:
            assert self._conn is not None
            with self._lock:
                for d in devices:
                    mac = d.get("mac")
                    if not mac:
                        continue
                    ip = d.get("ip") or ""
                    name = d.get("name") or ""
                    router = d.get("router") or ""
                    cur = self._conn.execute(
                        "INSERT OR IGNORE INTO devices"
                        "(mac,ip,name,router,first_seen,last_seen) "
                        "VALUES (?,?,?,?,?,?)",
                        (mac, ip, name, router, now, now))
                    if cur.rowcount:
                        new_macs.add(mac)
                    self._conn.execute(
                        "UPDATE devices SET ip=?, router=?, last_seen=?, "
                        "name=CASE WHEN ?!='' THEN ? ELSE name END "
                        "WHERE mac=?",
                        (ip, router, now, name, name, mac))
                self._conn.commit()

        import asyncio
        await asyncio.to_thread(_run)
        return new_macs

    async def list_devices(self, new_days: float = 7,
                           limit: int = 1000) -> List[Dict[str, Any]]:
        """Devices conhecidos, mais recentes primeiro.

        is_new = first_seen dentro dos ultimos `new_days` dias.
        """
        import asyncio
        cutoff = time.time() - new_days * 86400
        rows = await asyncio.to_thread(
            self._query,
            "SELECT mac, ip, name, router, first_seen, last_seen "
            "FROM devices ORDER BY last_seen DESC LIMIT ?",
            (limit,),
        )
        out = []
        for r in rows:
            d = dict(r)
            d["is_new"] = bool(d["first_seen"] >= cutoff)
            out.append(d)
        return out

    async def device_stats(self, new_days: float = 7) -> Dict[str, Any]:
        import asyncio
        now = time.time()
        new_cut = now - new_days * 86400
        day_cut = now - 86400
        total = await asyncio.to_thread(
            self._query, "SELECT COUNT(*) AS c FROM devices")
        new = await asyncio.to_thread(
            self._query,
            "SELECT COUNT(*) AS c FROM devices WHERE first_seen>=?",
            (new_cut,))
        seen24 = await asyncio.to_thread(
            self._query,
            "SELECT COUNT(*) AS c FROM devices WHERE last_seen>=?",
            (day_cut,))
        per_router = await asyncio.to_thread(
            self._query,
            "SELECT router, COUNT(*) AS c FROM devices GROUP BY router")
        return {
            "ts": now,
            "total": total[0]["c"],
            "new": new[0]["c"],
            "seen_24h": seen24[0]["c"],
            "by_router": {r["router"]: r["c"] for r in per_router},
        }

    # ---------- baselines ----------

    async def compute_baselines(self, routers: List[str],
                                days: int = 7,
                                ts: Optional[float] = None) -> int:
        """Median/p95/avg por roteador+metrica sobre samples_1m (7d).

        p95 = nearest-rank (indice ceil(0.95*n)-1). Retorna qtd de linhas
        inseridas na tabela `baselines`.
        """
        now = ts or time.time()
        cutoff = int(now) - days * 86400
        inserted = 0
        for router in routers:
            rows = await self.rollups_since(router, cutoff)
            for metric in _BASELINE_METRICS:
                vals = sorted(r["metric"].get(metric) for r in rows
                              if r["metric"].get(metric) is not None)
                if not vals:
                    continue
                n = len(vals)
                # mediana sem statistics p/ nao importar mais nada
                mid = n // 2
                median = (vals[mid] if n % 2
                          else (vals[mid - 1] + vals[mid]) / 2)
                p95 = vals[max(0, min(n - 1, -(-95 * n // 100) - 1))]
                avg = sum(vals) / n
                await self._insert_baseline(router, metric, now,
                                            median, p95, avg, n)
                inserted += 1
        return inserted

    async def _insert_baseline(self, router: str, metric: str,
                               computed_ts: float, median: float,
                               p95: float, avg: float, n: int) -> None:
        import asyncio
        await asyncio.to_thread(
            self._execute,
            "INSERT INTO baselines(router,metric,computed_ts,median,p95,"
            "avg,n) VALUES (?,?,?,?,?,?,?)",
            (router, metric, computed_ts, median, p95, avg, n),
        )

    async def latest_baselines(self) -> Dict[str, Dict[str, Any]]:
        """Ultima linha por (router, metric) -> {router: {metric: {...}}}."""
        import asyncio
        rows = await asyncio.to_thread(
            self._query,
            "SELECT b.router, b.metric, b.computed_ts, b.median, b.p95, "
            "b.avg, b.n FROM baselines b JOIN ("
            "  SELECT router, metric, MAX(computed_ts) AS m "
            "  FROM baselines GROUP BY router, metric"
            ") t ON b.router=t.router AND b.metric=t.metric "
            "AND b.computed_ts=t.m")
        out: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            out.setdefault(r["router"], {})[r["metric"]] = {
                "computed_ts": r["computed_ts"],
                "median": r["median"],
                "p95": r["p95"],
                "avg": r["avg"],
                "n": r["n"],
            }
        return out

    async def has_baselines(self) -> bool:
        import asyncio
        rows = await asyncio.to_thread(
            self._query, "SELECT COUNT(*) AS c FROM baselines")
        return bool(rows and rows[0]["c"])

    # ---------- events ----------

    async def insert_event(self, level: str, source: str, message: str,
                           ts: Optional[float] = None) -> int:
        import asyncio
        cur = await asyncio.to_thread(
            self._execute,
            "INSERT INTO events(ts, level, source, message) VALUES (?,?,?,?)",
            (ts or time.time(), level, source, message),
        )
        return int(cur.lastrowid)

    async def recent_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        import asyncio
        rows = await asyncio.to_thread(
            self._query,
            "SELECT id, ts, level, source, message FROM events "
            "ORDER BY ts DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    # ---------- actions ----------

    async def insert_action(self, kind: str, router: str,
                            payload: Dict[str, Any],
                            diff: List[List[str]],
                            status: str = "proposed") -> int:
        import asyncio
        cur = await asyncio.to_thread(
            self._execute,
            "INSERT INTO actions(ts, status, kind, router, payload, diff) "
            "VALUES (?,?,?,?,?,?)",
            (time.time(), status, kind, router, json.dumps(payload),
             json.dumps(diff)),
        )
        return int(cur.lastrowid)

    async def update_action(self, action_id: int,
                            status: Optional[str] = None,
                            approved_by: Optional[str] = None,
                            result: Optional[str] = None,
                            rollback: Optional[List[List[str]]] = None) -> None:
        import asyncio
        sets, params = [], []
        if status is not None:
            sets.append("status=?")
            params.append(status)
        if approved_by is not None:
            sets.append("approved_by=?")
            params.append(approved_by)
        if result is not None:
            sets.append("result=?")
            params.append(result)
        if rollback is not None:
            sets.append("rollback=?")
            params.append(json.dumps(rollback))
        if not sets:
            return
        params.append(action_id)
        await asyncio.to_thread(
            self._execute,
            "UPDATE actions SET %s WHERE id=?" % ", ".join(sets),
            tuple(params),
        )

    async def get_action(self, action_id: int) -> Optional[Dict[str, Any]]:
        import asyncio
        rows = await asyncio.to_thread(
            self._query, "SELECT * FROM actions WHERE id=?", (action_id,)
        )
        if not rows:
            return None
        r = dict(rows[0])
        r["payload"] = json.loads(r["payload"])
        r["diff"] = json.loads(r["diff"]) if r["diff"] else []
        r["rollback"] = json.loads(r["rollback"]) if r["rollback"] else []
        return r

    async def list_actions(self, limit: int = 50,
                           status: Optional[str] = None) -> List[Dict[str, Any]]:
        import asyncio
        if status:
            rows = await asyncio.to_thread(
                self._query,
                "SELECT * FROM actions WHERE status=? ORDER BY ts DESC LIMIT ?",
                (status, limit),
            )
        else:
            rows = await asyncio.to_thread(
                self._query,
                "SELECT * FROM actions ORDER BY ts DESC LIMIT ?", (limit,),
            )
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"])
            d["diff"] = json.loads(d["diff"]) if d["diff"] else []
            d["rollback"] = json.loads(d["rollback"]) if d["rollback"] else []
            out.append(d)
        return out

    # ---------- retencao ----------

    async def cleanup(self) -> Dict[str, int]:
        """Apaga samples crus > retention e rollups antigos. Retorna contagens."""
        import asyncio
        now = time.time()
        cutoff_samples = now - self.sample_retention_hours * 3600
        cutoff_rollup = int(now) - self.rollup_retention_days * 86400
        cutoff_ip = int(now) - self.ip_retention_days * 86400

        def _run() -> Dict[str, int]:
            assert self._conn is not None
            with self._lock:
                c1 = self._conn.execute(
                    "DELETE FROM samples WHERE ts<?", (cutoff_samples,))
                c2 = self._conn.execute(
                    "DELETE FROM samples_1m WHERE ts_min<?", (cutoff_rollup,))
                c3 = self._conn.execute(
                    "DELETE FROM baselines WHERE computed_ts<?",
                    (now - 90 * 86400,))
                c4 = self._conn.execute(
                    "DELETE FROM ip_1m WHERE ts_min<?", (cutoff_ip,))
                self._conn.commit()
                return {"samples": c1.rowcount, "rollups": c2.rowcount,
                        "baselines": c3.rowcount, "ip_1m": c4.rowcount}

        return await asyncio.to_thread(_run)
