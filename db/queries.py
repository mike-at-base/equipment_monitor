"""
All query functions used by the collector and the Dash app.
Every function that writes uses an explicit connection so the collector can
batch inserts.  Read functions open their own connection via Conn().
"""
from __future__ import annotations

import datetime
from typing import Any

import pandas as pd

from db.connection import Conn

# ── Config sync (collector startup) ─────────────────────────────────────────

def upsert_plc(name: str, endpoint: str, enabled: bool) -> int:
    with Conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO config_plc (name, opc_endpoint, enabled)
            VALUES (%s, %s, %s)
            ON CONFLICT (name) DO UPDATE
              SET opc_endpoint = EXCLUDED.opc_endpoint,
                  enabled      = EXCLUDED.enabled
            RETURNING id
            """,
            (name, endpoint, enabled),
        )
        return cur.fetchone()[0]


def upsert_em(plc_id: int, station: str, display_name: str,
              em_db_path: str, em_label: str, enabled: bool) -> int:
    with Conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO config_em
              (plc_id, station, display_name, em_db_path, em_label, enabled)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (plc_id, station, em_label) DO UPDATE
              SET display_name = EXCLUDED.display_name,
                  em_db_path   = EXCLUDED.em_db_path,
                  enabled      = EXCLUDED.enabled
            RETURNING id
            """,
            (plc_id, station, display_name, em_db_path, em_label, enabled),
        )
        return cur.fetchone()[0]


def upsert_sequence(em_id: int, seq_index: int,
                    seq_name: str, is_production: bool) -> None:
    with Conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO config_sequence
              (em_id, seq_index, seq_name, is_production)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (em_id, seq_index) DO UPDATE
              SET seq_name      = EXCLUDED.seq_name,
                  is_production = EXCLUDED.is_production
            """,
            (em_id, seq_index, seq_name, is_production),
        )


def get_em_id(plc_id: int, station: str, em_label: str) -> int | None:
    with Conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM config_em WHERE plc_id=%s AND station=%s AND em_label=%s",
            (plc_id, station, em_label),
        )
        row = cur.fetchone()
        return row[0] if row else None


def get_enabled_ems(plc_name: str) -> list[dict]:
    with Conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT e.id, e.station, e.display_name, e.em_db_path, e.em_label,
                   p.opc_endpoint
            FROM config_em e
            JOIN config_plc p ON p.id = e.plc_id
            WHERE p.name = %s AND e.enabled = TRUE AND p.enabled = TRUE
            """,
            (plc_name,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_sequences_for_em(em_id: int) -> list[dict]:
    with Conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT seq_index, seq_name, is_production FROM config_sequence WHERE em_id=%s ORDER BY seq_index",
            (em_id,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


# ── Collector writes ─────────────────────────────────────────────────────────

def insert_step_event(conn, em_id: int, seq_index: int, ts: datetime.datetime,
                      step_name: str, step_desc: str | None,
                      duration_ms: int | None, was_faulted: bool) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO step_event (ts, em_id, seq_index, step_name, step_desc,
                                duration_ms, was_faulted)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (ts, em_id, seq_index, step_name, step_desc, duration_ms, was_faulted),
    )


def insert_fault_start(conn, em_id: int, seq_index: int,
                       fault_start: datetime.datetime,
                       step_name: str | None, step_desc: str | None,
                       ext_msg: str | None) -> int:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO fault_event
          (fault_start, em_id, seq_index, step_name, step_desc, ext_fault_msg)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (fault_start, em_id, seq_index, step_name, step_desc, ext_msg),
    )
    return cur.fetchone()[0]


def close_fault(conn, fault_id: int, fault_end: datetime.datetime,
                duration_ms: int) -> None:
    cur = conn.cursor()
    cur.execute(
        "UPDATE fault_event SET fault_end=%s, duration_ms=%s WHERE id=%s",
        (fault_end, duration_ms, fault_id),
    )


def insert_availability_raw(conn, em_id: int, ts: datetime.datetime,
                            automatic: bool, fault: bool, running: bool) -> None:
    """Write one snapshot row whenever automatic, fault, or running changes."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO em_availability_raw (ts, em_id, automatic, fault, running)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (ts, em_id, automatic, fault, running),
    )


def upsert_current_step(conn, em_id: int, seq_index: int,
                        step_name: str, step_desc: str | None,
                        ts: datetime.datetime) -> None:
    """Keep em_current_step in sync with the arriving step (called from collector)."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO em_current_step (em_id, seq_index, step_name, step_desc, updated_at)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (em_id) DO UPDATE
          SET seq_index  = EXCLUDED.seq_index,
              step_name  = EXCLUDED.step_name,
              step_desc  = EXCLUDED.step_desc,
              updated_at = EXCLUDED.updated_at
        """,
        (em_id, seq_index, step_name, step_desc, ts),
    )


def update_heartbeat(conn, plc_name: str, connected: bool,
                     node_count: int = 0) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO collector_heartbeat (plc_name, last_seen, connected, node_count)
        VALUES (%s, NOW(), %s, %s)
        ON CONFLICT (plc_name) DO UPDATE
          SET last_seen  = EXCLUDED.last_seen,
              connected  = EXCLUDED.connected,
              node_count = EXCLUDED.node_count
        """,
        (plc_name, connected, node_count),
    )


# ── App reads ────────────────────────────────────────────────────────────────

def get_all_plcs() -> list[dict]:
    with Conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, name, opc_endpoint, enabled FROM config_plc ORDER BY name")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_stations_for_plc(plc_name: str) -> list[dict]:
    """Return distinct stations with their main-EM display names."""
    with Conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT DISTINCT ON (e.station)
                   e.station, e.display_name, e.enabled
            FROM config_em e
            JOIN config_plc p ON p.id = e.plc_id
            WHERE p.name = %s AND e.em_label = 'main'
            ORDER BY e.station
            """,
            (plc_name,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_ems_for_station(plc_name: str, station: str) -> list[dict]:
    with Conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT e.id, e.em_label, e.display_name
            FROM config_em e
            JOIN config_plc p ON p.id = e.plc_id
            WHERE p.name = %s AND e.station = %s
            ORDER BY e.em_label
            """,
            (plc_name, station),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def query_step_history(em_ids: list[int], seq_indices: list[int] | None,
                       start: datetime.datetime, end: datetime.datetime,
                       limit: int = 2000) -> pd.DataFrame:
    with Conn() as conn:
        cur = conn.cursor()
        seq_clause = ""
        params: list[Any] = [em_ids, start, end]
        if seq_indices:
            seq_clause = "AND s.seq_index = ANY(%s)"
            params.insert(2, seq_indices)
        cur.execute(
            f"""
            SELECT s.ts, e.station, e.em_label, cs.seq_name,
                   s.step_name, s.step_desc, s.duration_ms, s.was_faulted
            FROM step_event s
            JOIN config_em e ON e.id = s.em_id
            JOIN config_sequence cs ON cs.em_id = s.em_id
                                   AND cs.seq_index = s.seq_index
            WHERE s.em_id = ANY(%s)
              AND s.ts BETWEEN %s AND %s
              {seq_clause}
            ORDER BY s.ts DESC
            LIMIT %s
            """,
            params + [limit],
        )
        cols = [d[0] for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)


def query_cycle_times(em_id: int, seq_index: int,
                      start: datetime.datetime, end: datetime.datetime) -> pd.DataFrame:
    with Conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT ts,
                   EXTRACT(EPOCH FROM
                       ts - LAG(ts) OVER (ORDER BY ts)
                   ) * 1000 AS cycle_ms
            FROM step_event
            WHERE em_id = %s
              AND seq_index = %s
              AND step_name = 'SEQUENCE_INITIAL_STEP'
              AND ts BETWEEN %s AND %s
            ORDER BY ts
            """,
            (em_id, seq_index, start, end),
        )
        df = pd.DataFrame(cur.fetchall(), columns=["ts", "cycle_ms"])
        return df.dropna(subset=["cycle_ms"])


def query_fault_pareto(em_ids: list[int], seq_indices: list[int] | None,
                       start: datetime.datetime, end: datetime.datetime) -> pd.DataFrame:
    with Conn() as conn:
        cur = conn.cursor()
        seq_clause = ""
        params: list[Any] = [em_ids, start, end]
        if seq_indices:
            seq_clause = "AND seq_index = ANY(%s)"
            params.insert(2, seq_indices)
        cur.execute(
            f"""
            SELECT step_name, step_desc,
                   COUNT(*) AS fault_count,
                   AVG(duration_ms) AS avg_duration_ms,
                   SUM(duration_ms) AS total_duration_ms
            FROM fault_event
            WHERE em_id = ANY(%s)
              AND fault_start BETWEEN %s AND %s
              {seq_clause}
            GROUP BY step_name, step_desc
            ORDER BY fault_count DESC
            """,
            params,
        )
        cols = [d[0] for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)


def query_fault_events(em_ids: list[int], seq_indices: list[int] | None,
                       start: datetime.datetime, end: datetime.datetime) -> pd.DataFrame:
    with Conn() as conn:
        cur = conn.cursor()
        seq_clause = ""
        params: list[Any] = [em_ids, start, end]
        if seq_indices:
            seq_clause = "AND f.seq_index = ANY(%s)"
            params.insert(2, seq_indices)
        cur.execute(
            f"""
            SELECT f.fault_start, f.fault_end, f.duration_ms,
                   e.station, e.em_label, cs.seq_name,
                   f.step_name, f.step_desc, f.ext_fault_msg
            FROM fault_event f
            JOIN config_em e ON e.id = f.em_id
            JOIN config_sequence cs ON cs.em_id = f.em_id
                                    AND cs.seq_index = f.seq_index
            WHERE f.em_id = ANY(%s)
              AND f.fault_start BETWEEN %s AND %s
              {seq_clause}
            ORDER BY f.fault_start DESC
            """,
            params,
        )
        cols = [d[0] for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)


def query_fault_frequency(em_ids: list[int], start: datetime.datetime,
                          end: datetime.datetime,
                          bucket: str = "1 hour") -> pd.DataFrame:
    with Conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT time_bucket('{bucket}', fault_start) AS bucket,
                   COUNT(*) AS fault_count
            FROM fault_event
            WHERE em_id = ANY(%s)
              AND fault_start BETWEEN %s AND %s
            GROUP BY bucket
            ORDER BY bucket
            """,
            (em_ids, start, end),
        )
        return pd.DataFrame(cur.fetchall(), columns=["bucket", "fault_count"])


def query_state_timeline(em_ids: list[int],
                         start: datetime.datetime,
                         end: datetime.datetime) -> pd.DataFrame:
    """
    Return one row per raw signal-change interval with SEMI E10 state label.
    Used by the Gantt chart.

    States:
      productive      — automatic=T, running=T, fault=F
      standby         — automatic=T, running=F, fault=F
      unscheduled_down — fault=T (any mode)
      manual          — automatic=F, fault=F
    """
    with Conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            WITH raw AS (
                SELECT ts, em_id, automatic, fault, running,
                       LEAD(ts) OVER (PARTITION BY em_id ORDER BY ts) AS next_ts,
                       CASE
                         WHEN fault                     THEN 'unscheduled_down'
                         WHEN automatic AND running     THEN 'productive'
                         WHEN automatic AND NOT running THEN 'standby'
                         ELSE 'manual'
                       END AS state
                FROM em_availability_raw
                WHERE em_id = ANY(%s)
                  AND ts <= %s
            )
            SELECT r.ts, r.em_id, r.state, r.next_ts,
                   e.station, e.display_name, e.em_label
            FROM raw r
            JOIN config_em e ON e.id = r.em_id
            WHERE (r.next_ts IS NULL OR r.next_ts > %s)
            ORDER BY r.em_id, r.ts
            """,
            (em_ids, end, start),
        )
        cols = [d[0] for d in cur.description]
        df = pd.DataFrame(cur.fetchall(), columns=cols)
    if df.empty:
        return df
    # Clip intervals to the requested [start, end] window
    end_ts   = pd.Timestamp(end)
    start_ts = pd.Timestamp(start)
    df["next_ts"] = df["next_ts"].fillna(end_ts)
    df["next_ts"] = df["next_ts"].clip(upper=end_ts)
    df["ts"]      = df["ts"].clip(lower=start_ts)
    return df[df["next_ts"] > df["ts"]].reset_index(drop=True)


def query_state_summary(em_ids: list[int],
                        start: datetime.datetime,
                        end: datetime.datetime) -> pd.DataFrame:
    """
    Return SEMI E10 time summary per EM: productive / standby / down / manual
    minutes and availability %.

    Availability % = (productive + standby) / (productive + standby + down) × 100
    Manual time is excluded from the denominator (SEMI E10 Non-Scheduled Time).
    """
    with Conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            WITH raw AS (
                SELECT ts, em_id,
                       LEAD(ts) OVER (PARTITION BY em_id ORDER BY ts) AS next_ts,
                       CASE
                         WHEN fault                     THEN 'unscheduled_down'
                         WHEN automatic AND running     THEN 'productive'
                         WHEN automatic AND NOT running THEN 'standby'
                         ELSE 'manual'
                       END AS state
                FROM em_availability_raw
                WHERE em_id = ANY(%s)
                  AND ts <= %s
            ),
            clipped AS (
                SELECT em_id, state,
                       GREATEST(ts,      %s::timestamptz) AS t_start,
                       LEAST(COALESCE(next_ts, %s::timestamptz),
                             %s::timestamptz)              AS t_end
                FROM raw
                WHERE (next_ts IS NULL OR next_ts > %s)
                  AND ts < %s
            )
            SELECT c.em_id,
                   e.station, e.display_name, e.em_label,
                   ROUND(SUM(EXTRACT(EPOCH FROM t_end-t_start))
                         FILTER (WHERE state = 'productive'       ) / 60.0, 1) AS productive_min,
                   ROUND(SUM(EXTRACT(EPOCH FROM t_end-t_start))
                         FILTER (WHERE state = 'standby'          ) / 60.0, 1) AS standby_min,
                   ROUND(SUM(EXTRACT(EPOCH FROM t_end-t_start))
                         FILTER (WHERE state = 'unscheduled_down' ) / 60.0, 1) AS down_min,
                   ROUND(SUM(EXTRACT(EPOCH FROM t_end-t_start))
                         FILTER (WHERE state = 'manual'           ) / 60.0, 1) AS manual_min,
                   ROUND(
                       NULLIF(SUM(EXTRACT(EPOCH FROM t_end-t_start))
                              FILTER (WHERE state IN ('productive','standby')), 0) /
                       NULLIF(SUM(EXTRACT(EPOCH FROM t_end-t_start))
                              FILTER (WHERE state IN ('productive','standby','unscheduled_down')), 0)
                       * 100.0, 1
                   ) AS availability_pct
            FROM clipped c
            JOIN config_em e ON e.id = c.em_id
            WHERE t_end > t_start
            GROUP BY c.em_id, e.station, e.display_name, e.em_label
            ORDER BY e.station, e.em_label
            """,
            (em_ids, end, start, end, end, start, end),
        )
        cols = [d[0] for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)


def query_station_status(plc_name: str) -> list[dict]:
    """
    Return one row per enabled EM with:
      - SEMI E10 state derived from the latest em_availability_raw row
      - Current step from em_current_step (upserted by collector on each step change)

    Ordered by station then em_label (main before robot EMs).
    """
    with Conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                e.station,
                e.display_name,
                e.em_label,
                e.id AS em_id,
                CASE
                    WHEN r.ts IS NULL                  THEN 'unknown'
                    WHEN r.fault                       THEN 'unscheduled_down'
                    WHEN r.automatic AND r.running     THEN 'productive'
                    WHEN r.automatic AND NOT r.running THEN 'standby'
                    ELSE                                    'manual'
                END AS state,
                cs.step_name,
                cs.step_desc,
                cs.seq_index,
                csq.seq_name,
                cs.updated_at
            FROM config_em e
            JOIN config_plc p ON p.id = e.plc_id
            LEFT JOIN LATERAL (
                SELECT automatic, fault, running, ts
                FROM em_availability_raw
                WHERE em_id = e.id
                ORDER BY ts DESC
                LIMIT 1
            ) r ON true
            LEFT JOIN em_current_step cs ON cs.em_id = e.id
            LEFT JOIN config_sequence csq
                   ON csq.em_id = e.id AND csq.seq_index = cs.seq_index
            WHERE p.name = %s
              AND e.enabled = TRUE
            ORDER BY e.station,
                     CASE WHEN e.em_label = 'main' THEN 0 ELSE 1 END,
                     e.em_label
            """,
            (plc_name,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_collector_status() -> pd.DataFrame:
    with Conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT plc_name, last_seen, connected, node_count FROM collector_heartbeat"
        )
        cols = [d[0] for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)
