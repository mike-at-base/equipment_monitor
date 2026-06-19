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
                    seq_name: str, is_production: bool,
                    cycle_start_step: str = "SEQUENCE_INITIAL_STEP") -> None:
    with Conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO config_sequence
              (em_id, seq_index, seq_name, is_production, cycle_start_step)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (em_id, seq_index) DO UPDATE
              SET seq_name      = EXCLUDED.seq_name,
                  is_production = EXCLUDED.is_production,
                  cycle_start_step = EXCLUDED.cycle_start_step
            """,
            (em_id, seq_index, seq_name, is_production, cycle_start_step),
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
            "SELECT seq_index, seq_name, is_production, cycle_start_step "
            "FROM config_sequence WHERE em_id=%s ORDER BY seq_index",
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


def get_open_fault(conn, em_id: int, seq_index: int) -> dict | None:
    """Return the latest open fault row for one EM/sequence, if any."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, fault_start, step_name, step_desc, ext_fault_msg
        FROM fault_event
        WHERE em_id = %s
          AND seq_index = %s
          AND fault_end IS NULL
        ORDER BY fault_start DESC
        LIMIT 1
        """,
        (em_id, seq_index),
    )
    row = cur.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


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
        WHERE em_current_step.seq_index IS DISTINCT FROM EXCLUDED.seq_index
           OR em_current_step.step_name IS DISTINCT FROM EXCLUDED.step_name
           OR em_current_step.step_desc IS DISTINCT FROM EXCLUDED.step_desc
        """,
        (em_id, seq_index, step_name, step_desc, ts),
    )


def open_down_event(conn, em_id: int, start_ts: datetime.datetime,
                    reason_type: str, reason_desc: str | None,
                    seq_index: int | None, step_name: str | None,
                    fault_msg: str | None) -> None:
    """Open a new down event.  end_ts is NULL until the machine recovers."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO em_down_event
          (start_ts, em_id, reason_type, reason_desc, seq_index, step_name, fault_msg)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (start_ts, em_id, reason_type, reason_desc, seq_index, step_name, fault_msg),
    )


def close_down_event(conn, em_id: int, start_ts: datetime.datetime,
                     end_ts: datetime.datetime) -> None:
    """Close an open down event identified by (em_id, start_ts)."""
    dur_ms = int((end_ts - start_ts).total_seconds() * 1000)
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE em_down_event
           SET end_ts = %s, duration_ms = %s
         WHERE em_id = %s AND start_ts = %s AND end_ts IS NULL
        """,
        (end_ts, dur_ms, em_id, start_ts),
    )


def get_open_down_event(conn, em_id: int) -> dict | None:
    """Return the latest open down-event row for one EM, if any."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT start_ts, reason_type, reason_desc, seq_index, step_name, fault_msg
        FROM em_down_event
        WHERE em_id = %s
          AND end_ts IS NULL
        ORDER BY start_ts DESC
        LIMIT 1
        """,
        (em_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def update_down_event_reason(conn, em_id: int, start_ts: datetime.datetime,
                             reason_desc: str,
                             reason_type: str | None = None) -> None:
    """
    Patch the reason fields on the currently-open down event.  Used by the
    OpcClient after an async on-demand struct read enriches the placeholder.
    If ``reason_type`` is provided, it is updated alongside the description
    (e.g. demoting 'interlock' → 'manual' when no interlock condition was
    actually failing).  No-op if the event has already been closed.
    """
    cur = conn.cursor()
    if reason_type is not None:
        cur.execute(
            """
            UPDATE em_down_event
               SET reason_desc = %s,
                   reason_type = %s
             WHERE em_id    = %s
               AND start_ts = %s
               AND end_ts IS NULL
            """,
            (reason_desc, reason_type, em_id, start_ts),
        )
    else:
        cur.execute(
            """
            UPDATE em_down_event
               SET reason_desc = %s
             WHERE em_id    = %s
               AND start_ts = %s
               AND end_ts IS NULL
            """,
            (reason_desc, em_id, start_ts),
        )


def update_down_event_context(conn, em_id: int, start_ts: datetime.datetime,
                              seq_index: int | None, step_name: str | None,
                              fault_msg: str | None) -> None:
    """
    Patch sequence context on an open down event.
    Used when a generic EM-fault event opens first and the sequence-level
    fault edge arrives shortly after.
    """
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE em_down_event
           SET seq_index = %s,
               step_name = %s,
               fault_msg = %s
         WHERE em_id    = %s
           AND start_ts = %s
           AND end_ts IS NULL
        """,
        (seq_index, step_name, fault_msg, em_id, start_ts),
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

def query_down_events(em_ids: list[int],
                      start: datetime.datetime,
                      end: datetime.datetime,
                      limit: int = 500) -> pd.DataFrame:
    """
    Return down events that overlap [start, end].
    Includes open events (end_ts IS NULL) so live faults appear immediately.
    """
    with Conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT d.start_ts, d.end_ts, d.duration_ms,
                   e.station, e.display_name, e.em_label,
                   d.reason_type, d.reason_desc, d.step_name, d.fault_msg
            FROM em_down_event d
            JOIN config_em e ON e.id = d.em_id
            WHERE d.em_id = ANY(%s)
              AND d.start_ts < %s
              AND (d.end_ts IS NULL OR d.end_ts > %s)
            ORDER BY d.start_ts DESC
            LIMIT %s
            """,
            (em_ids, end, start, limit),
        )
        cols = [d[0] for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)


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
                       limit: int | None = 2000) -> pd.DataFrame:
    with Conn() as conn:
        cur = conn.cursor()
        seq_clause = ""
        params: list[Any] = [em_ids, start, end]
        if seq_indices:
            seq_clause = "AND s.seq_index = ANY(%s)"
            params.insert(2, seq_indices)
        limit_clause = "LIMIT %s" if limit is not None else ""
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
            {limit_clause}
            """,
            params + ([limit] if limit is not None else []),
        )
        cols = [d[0] for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)


def query_cycle_times(em_id: int, seq_index: int,
                      cycle_start_step: str,
                      start: datetime.datetime, end: datetime.datetime) -> pd.DataFrame:
    with Conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            WITH ordered AS (
                SELECT ts,
                       LEAD(step_name) OVER (ORDER BY ts) AS arriving_step
                FROM step_event
                WHERE em_id = %s
                  AND seq_index = %s
            ),
            cycle_starts AS (
                -- step_event stores DEPARTING steps at transition time.
                -- The ARRIVING step at ts is represented by the next row's
                -- departing step (LEAD(step_name)).
                -- Start-to-start cycle time = delta between consecutive
                -- transitions where the arriving step is INITIAL_STEP.
                SELECT ts
                FROM ordered
                WHERE arriving_step = %s
            ),
            cycles AS (
                SELECT
                    LAG(ts) OVER (ORDER BY ts) AS cycle_start_ts,
                    ts AS cycle_end_ts,
                    EXTRACT(EPOCH FROM ts - LAG(ts) OVER (ORDER BY ts)) * 1000 AS cycle_ms_raw
                FROM cycle_starts
            ),
            stop_sums AS (
                SELECT
                    c.cycle_end_ts,
                    COALESCE(SUM(s.duration_ms), 0) AS step_stop_ms
                FROM cycles c
                LEFT JOIN step_event s
                  ON s.em_id = %s
                 AND s.seq_index = %s
                 AND s.ts > c.cycle_start_ts
                 AND s.ts <= c.cycle_end_ts
                 AND s.step_name = 'STEP_STOP'
                WHERE c.cycle_start_ts IS NOT NULL
                GROUP BY c.cycle_end_ts
            )
            SELECT
                c.cycle_end_ts AS ts,
                GREATEST(0, c.cycle_ms_raw - COALESCE(ss.step_stop_ms, 0)) AS cycle_ms
            FROM cycles c
            LEFT JOIN stop_sums ss
              ON ss.cycle_end_ts = c.cycle_end_ts
            WHERE c.cycle_start_ts IS NOT NULL
              AND c.cycle_end_ts BETWEEN %s AND %s
            ORDER BY c.cycle_end_ts
            """,
            (em_id, seq_index, cycle_start_step, em_id, seq_index, start, end),
        )
        df = pd.DataFrame(cur.fetchall(), columns=["ts", "cycle_ms"])
        return df.dropna(subset=["cycle_ms"])


def query_cycle_windows(em_id: int, seq_index: int,
                        cycle_start_step: str,
                        start: datetime.datetime,
                        end: datetime.datetime) -> pd.DataFrame:
    """
    Return one row per completed cycle:
      - cycle_start_ts: previous arrival to cycle_start_step
      - cycle_end_ts: current arrival to cycle_start_step
      - cycle_ms: start-to-start duration
    """
    with Conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            WITH ordered AS (
                SELECT ts,
                       LEAD(step_name) OVER (ORDER BY ts) AS arriving_step
                FROM step_event
                WHERE em_id = %s
                  AND seq_index = %s
            ),
            cycle_starts AS (
                SELECT ts
                FROM ordered
                WHERE arriving_step = %s
            ),
            cycles AS (
                SELECT
                    LAG(ts) OVER (ORDER BY ts) AS cycle_start_ts,
                    ts AS cycle_end_ts,
                    EXTRACT(EPOCH FROM ts - LAG(ts) OVER (ORDER BY ts)) * 1000 AS cycle_ms_raw
                FROM cycle_starts
            ),
            stop_sums AS (
                SELECT
                    c.cycle_end_ts,
                    COALESCE(SUM(s.duration_ms), 0) AS step_stop_ms
                FROM cycles c
                LEFT JOIN step_event s
                  ON s.em_id = %s
                 AND s.seq_index = %s
                 AND s.ts > c.cycle_start_ts
                 AND s.ts <= c.cycle_end_ts
                 AND s.step_name = 'STEP_STOP'
                WHERE c.cycle_start_ts IS NOT NULL
                GROUP BY c.cycle_end_ts
            )
            SELECT
                c.cycle_start_ts,
                c.cycle_end_ts,
                GREATEST(0, c.cycle_ms_raw - COALESCE(ss.step_stop_ms, 0)) AS cycle_ms
            FROM cycles c
            LEFT JOIN stop_sums ss
              ON ss.cycle_end_ts = c.cycle_end_ts
            WHERE c.cycle_start_ts IS NOT NULL
              AND c.cycle_end_ts BETWEEN %s AND %s
            ORDER BY cycle_end_ts DESC
            """,
            (em_id, seq_index, cycle_start_step, em_id, seq_index, start, end),
        )
        return pd.DataFrame(
            cur.fetchall(),
            columns=["cycle_start_ts", "cycle_end_ts", "cycle_ms"],
        )


def query_cycle_steps(em_id: int, seq_index: int,
                      cycle_start_ts: datetime.datetime,
                      cycle_end_ts: datetime.datetime) -> pd.DataFrame:
    """
    Step transitions that occurred within one cycle window (start, end].
    step_event rows represent DEPARTING step durations at transition time.
    """
    with Conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT ts, step_name, step_desc, duration_ms, was_faulted
            FROM step_event
            WHERE em_id = %s
              AND seq_index = %s
              AND ts > %s
              AND ts <= %s
            ORDER BY ts
            """,
            (em_id, seq_index, cycle_start_ts, cycle_end_ts),
        )
        return pd.DataFrame(
            cur.fetchall(),
            columns=["ts", "step_name", "step_desc", "duration_ms", "was_faulted"],
        )


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


def query_fault_pareto_detailed(em_ids: list[int],
                                start: datetime.datetime,
                                end: datetime.datetime) -> pd.DataFrame:
    """Fault pareto attributed to the station + sequence each fault came from.

    Like query_fault_pareto but grouped by station/em/sequence as well as the
    step, so callers can show where downtime originated. LEFT JOIN on
    config_sequence so faults on an unknown sequence still appear.
    """
    with Conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT e.station, e.em_label, e.display_name, cs.seq_name,
                   f.step_name, f.step_desc,
                   COUNT(*) AS fault_count,
                   AVG(f.duration_ms) AS avg_duration_ms,
                   SUM(f.duration_ms) AS total_duration_ms
            FROM fault_event f
            JOIN config_em e ON e.id = f.em_id
            LEFT JOIN config_sequence cs ON cs.em_id = f.em_id
                                         AND cs.seq_index = f.seq_index
            WHERE f.em_id = ANY(%s)
              AND f.fault_start BETWEEN %s AND %s
            GROUP BY e.station, e.em_label, e.display_name, cs.seq_name,
                     f.step_name, f.step_desc
            ORDER BY total_duration_ms DESC NULLS LAST
            """,
            (em_ids, start, end),
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
                -- Some PLC bursts can produce multiple raw rows with identical
                -- source timestamps (e.g. running false/true in one cycle).
                -- Prefer fault=true first, then running=true for ties so the
                -- dashboard avoids random standby/prod flips on equal ts.
                ORDER BY ts DESC, fault DESC, running DESC, automatic DESC
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
