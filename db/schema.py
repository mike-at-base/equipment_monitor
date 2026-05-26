"""
Run once to create all tables and hypertables.
    python db/schema.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db.connection import Conn

DDL = """
-- ── Configuration tables (populated from config.yaml on collector startup) ──

CREATE TABLE IF NOT EXISTS config_plc (
    id       SERIAL PRIMARY KEY,
    name     TEXT UNIQUE NOT NULL,
    opc_endpoint TEXT NOT NULL,
    enabled  BOOL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS config_em (
    id           SERIAL PRIMARY KEY,
    plc_id       INT NOT NULL REFERENCES config_plc(id) ON DELETE CASCADE,
    station      TEXT NOT NULL,
    display_name TEXT NOT NULL,
    em_db_path   TEXT NOT NULL,
    em_label     TEXT NOT NULL,
    enabled      BOOL DEFAULT TRUE,
    UNIQUE (plc_id, station, em_label)
);

CREATE TABLE IF NOT EXISTS config_sequence (
    id            SERIAL PRIMARY KEY,
    em_id         INT NOT NULL REFERENCES config_em(id) ON DELETE CASCADE,
    seq_index     SMALLINT NOT NULL,
    seq_name      TEXT NOT NULL,
    is_production BOOL DEFAULT FALSE,
    cycle_start_step TEXT DEFAULT 'SEQUENCE_INITIAL_STEP',
    UNIQUE (em_id, seq_index)
);

-- ── Collector heartbeat (app polls this to show connection status) ──────────

CREATE TABLE IF NOT EXISTS collector_heartbeat (
    plc_name    TEXT PRIMARY KEY,
    last_seen   TIMESTAMPTZ,
    connected   BOOL DEFAULT FALSE,
    node_count  INT DEFAULT 0
);

-- ── Time-series event tables ─────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS step_event (
    ts          TIMESTAMPTZ NOT NULL,
    em_id       INT NOT NULL,
    seq_index   SMALLINT NOT NULL,
    step_name   TEXT NOT NULL,
    step_desc   TEXT,
    duration_ms INT,
    was_faulted BOOL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS fault_event (
    id            BIGSERIAL,
    fault_start   TIMESTAMPTZ NOT NULL,
    fault_end     TIMESTAMPTZ,
    duration_ms   INT,
    em_id         INT NOT NULL,
    seq_index     SMALLINT NOT NULL,
    step_name     TEXT,
    step_desc     TEXT,
    ext_fault_msg TEXT
);

CREATE TABLE IF NOT EXISTS em_availability_raw (
    ts        TIMESTAMPTZ NOT NULL,
    em_id     INT NOT NULL,
    automatic BOOL NOT NULL,
    fault     BOOL NOT NULL,
    running   BOOL NOT NULL
);

-- ── Live status snapshot (one row per EM, upserted on every step change) ────
-- Gives the status dashboard the arriving step without a one-behind lag.

CREATE TABLE IF NOT EXISTS em_current_step (
    em_id      INT PRIMARY KEY REFERENCES config_em(id) ON DELETE CASCADE,
    seq_index  SMALLINT    NOT NULL,
    step_name  TEXT        NOT NULL,
    step_desc  TEXT,
    updated_at TIMESTAMPTZ NOT NULL
);

-- ── Down event tracking — sticky root-cause unavailability record ─────────────
-- One open row per EM while unavailable; closed when productive/standby resumes.
-- Reason is locked at the first cause (step fault or interlock); secondary events
-- (door open, mode changes) do NOT overwrite it — they are excluded from this table.
--
-- reason_type: 'step_fault' | 'interlock' | 'manual' | 'unknown'
-- reason_desc: human-readable concat of failed permissive/interlock descriptions

CREATE TABLE IF NOT EXISTS em_down_event (
    start_ts    TIMESTAMPTZ NOT NULL,
    em_id       INT NOT NULL,
    end_ts      TIMESTAMPTZ,
    duration_ms INT,
    reason_type TEXT NOT NULL,
    reason_desc TEXT,
    seq_index   SMALLINT,
    step_name   TEXT,
    fault_msg   TEXT
);
"""

HYPERTABLES = [
    ("step_event",          "ts"),
    ("fault_event",         "fault_start"),
    ("em_availability_raw", "ts"),
    ("em_down_event",       "start_ts"),
]

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_step_em_seq_ts   ON step_event          (em_id, seq_index, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_fault_em_seq     ON fault_event         (em_id, seq_index, fault_start DESC)",
    "CREATE INDEX IF NOT EXISTS idx_avail_raw_em_ts  ON em_availability_raw (em_id, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_down_em_start    ON em_down_event       (em_id, start_ts DESC)",
]


def init_schema():
    with Conn() as conn:
        cur = conn.cursor()
        # Migration: drop old binary availability table if upgrading from previous schema
        cur.execute("DROP TABLE IF EXISTS availability_event CASCADE")
        # em_down_event was added in the SEMI E10 redesign — no destructive migration needed
        cur.execute(DDL)
        # Migration: add cycle-start step setting for configurable cycle metrics.
        cur.execute(
            "ALTER TABLE config_sequence "
            "ADD COLUMN IF NOT EXISTS cycle_start_step TEXT "
            "DEFAULT 'SEQUENCE_INITIAL_STEP'"
        )
        for table, col in HYPERTABLES:
            try:
                cur.execute(
                    f"SELECT create_hypertable('{table}', '{col}', if_not_exists => TRUE)"
                )
            except Exception as e:
                print(f"  hypertable {table}: {e}")
        for idx in INDEXES:
            cur.execute(idx)
    print("Schema initialised.")


if __name__ == "__main__":
    init_schema()
