// Package store is the batching TimescaleDB writer. The tracker emits
// append-only rows into a channel; a single writer goroutine flushes them
// in batches (time- or size-triggered), so the ingest path never blocks on
// the database — the architectural fix over the v1 collector.
package store

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/mike-at-base/equipment_monitor/hub/internal/model"
)

const (
	flushInterval = 100 * time.Millisecond
	flushSize     = 500
	queueDepth    = 10000
)

var ddl = []string{
	`CREATE TABLE IF NOT EXISTS line (
	    id SERIAL PRIMARY KEY,
	    name TEXT UNIQUE NOT NULL,
	    plc_host TEXT NOT NULL DEFAULT '',
	    enabled BOOL NOT NULL DEFAULT TRUE)`,
	`CREATE TABLE IF NOT EXISTS em (
	    id SERIAL PRIMARY KEY,
	    line_id INT NOT NULL REFERENCES line(id) ON DELETE CASCADE,
	    station TEXT NOT NULL,
	    em_label TEXT NOT NULL,
	    display_name TEXT NOT NULL DEFAULT '',
	    enabled BOOL NOT NULL DEFAULT TRUE,
	    UNIQUE (line_id, station, em_label))`,
	`CREATE TABLE IF NOT EXISTS sequence (
	    id SERIAL PRIMARY KEY,
	    em_id INT NOT NULL REFERENCES em(id) ON DELETE CASCADE,
	    seq_index SMALLINT NOT NULL,
	    name TEXT NOT NULL DEFAULT '',
	    is_production BOOL NOT NULL DEFAULT FALSE,
	    cycle_start_step TEXT NOT NULL DEFAULT '',
	    cycle_complete_step TEXT NOT NULL DEFAULT '',
	    UNIQUE (em_id, seq_index))`,
	`CREATE TABLE IF NOT EXISTS state_interval (
	    start_ts TIMESTAMPTZ NOT NULL,
	    em_id INT NOT NULL,
	    end_ts TIMESTAMPTZ NOT NULL,
	    state TEXT NOT NULL,
	    reason_type TEXT NOT NULL DEFAULT '',
	    reason TEXT NOT NULL DEFAULT '',
	    seq_index SMALLINT,
	    step_name TEXT,
	    ack_ts TIMESTAMPTZ)`,
	`CREATE TABLE IF NOT EXISTS step_event (
	    start_ts TIMESTAMPTZ NOT NULL,
	    em_id INT NOT NULL,
	    end_ts TIMESTAMPTZ NOT NULL,
	    seq_index SMALLINT NOT NULL,
	    step_name TEXT NOT NULL,
	    step_desc TEXT NOT NULL DEFAULT '',
	    duration_ms BIGINT NOT NULL,
	    was_faulted BOOL NOT NULL DEFAULT FALSE)`,
	`CREATE TABLE IF NOT EXISTS cycle (
	    start_ts TIMESTAMPTZ NOT NULL,
	    em_id INT NOT NULL,
	    end_ts TIMESTAMPTZ NOT NULL,
	    seq_index SMALLINT NOT NULL,
	    work_end_ts TIMESTAMPTZ,
	    work_ms BIGINT,
	    exchange_ms BIGINT,
	    total_ms BIGINT NOT NULL,
	    interrupted BOOL NOT NULL DEFAULT FALSE)`,
	`CREATE TABLE IF NOT EXISTS mode_interval (
	    start_ts TIMESTAMPTZ NOT NULL,
	    em_id INT NOT NULL,
	    end_ts TIMESTAMPTZ NOT NULL,
	    flag TEXT NOT NULL)`,
	`CREATE TABLE IF NOT EXISTS operator_event (
	    ts TIMESTAMPTZ NOT NULL,
	    em_id INT NOT NULL,
	    event TEXT NOT NULL)`,
}

var hypertables = [][2]string{
	{"state_interval", "start_ts"},
	{"step_event", "start_ts"},
	{"cycle", "start_ts"},
	{"mode_interval", "start_ts"},
	{"operator_event", "ts"},
}

var indexes = []string{
	`CREATE INDEX IF NOT EXISTS idx_state_em ON state_interval (em_id, start_ts DESC)`,
	`CREATE INDEX IF NOT EXISTS idx_step_em ON step_event (em_id, seq_index, start_ts DESC)`,
	`CREATE INDEX IF NOT EXISTS idx_cycle_em ON cycle (em_id, start_ts DESC)`,
	`CREATE INDEX IF NOT EXISTS idx_mode_em ON mode_interval (em_id, start_ts DESC)`,
	`CREATE INDEX IF NOT EXISTS idx_oper_em ON operator_event (em_id, ts DESC)`,
}

type row struct {
	kind byte // s=state e=step c=cycle m=mode o=operator
	si   model.StateInterval
	se   model.StepEvent
	cy   model.Cycle
	mi   model.ModeInterval
	oe   model.OperatorEvent
}

type PG struct {
	pool *pgxpool.Pool
	ch   chan row
	log  *slog.Logger
	done chan struct{}
}

func Open(ctx context.Context, dsn string, log *slog.Logger) (*PG, error) {
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		return nil, err
	}
	if err := pool.Ping(ctx); err != nil {
		return nil, fmt.Errorf("db ping: %w", err)
	}
	p := &PG{pool: pool, ch: make(chan row, queueDepth), log: log,
		done: make(chan struct{})}
	return p, nil
}

func (p *PG) InitSchema(ctx context.Context) error {
	for _, stmt := range ddl {
		if _, err := p.pool.Exec(ctx, stmt); err != nil {
			return fmt.Errorf("ddl: %w", err)
		}
	}
	for _, ht := range hypertables {
		_, err := p.pool.Exec(ctx, fmt.Sprintf(
			`SELECT create_hypertable('%s','%s', if_not_exists => TRUE)`, ht[0], ht[1]))
		if err != nil {
			p.log.Warn("hypertable", "table", ht[0], "err", err)
		}
	}
	for _, idx := range indexes {
		if _, err := p.pool.Exec(ctx, idx); err != nil {
			return fmt.Errorf("index: %w", err)
		}
	}
	return nil
}

// SyncConfig upserts the hierarchy and returns em ids keyed by
// (lower(station), lower(em_label)) per line name.
func (p *PG) SyncConfig(ctx context.Context, lines []LineConfig) (map[string]map[[2]string]int, error) {
	out := map[string]map[[2]string]int{}
	for _, l := range lines {
		var lineID int
		err := p.pool.QueryRow(ctx, `
		    INSERT INTO line (name, plc_host, enabled) VALUES ($1,$2,$3)
		    ON CONFLICT (name) DO UPDATE SET plc_host=$2, enabled=$3
		    RETURNING id`, l.Name, l.Host, l.Enabled).Scan(&lineID)
		if err != nil {
			return nil, err
		}
		out[l.Name] = map[[2]string]int{}
		for _, e := range l.EMs {
			var emID int
			err := p.pool.QueryRow(ctx, `
			    INSERT INTO em (line_id, station, em_label, display_name, enabled)
			    VALUES ($1,$2,$3,$4,$5)
			    ON CONFLICT (line_id, station, em_label)
			    DO UPDATE SET display_name=$4, enabled=$5
			    RETURNING id`,
				lineID, e.Station, e.EMLabel, e.DisplayName, e.Enabled).Scan(&emID)
			if err != nil {
				return nil, err
			}
			out[l.Name][[2]string{lower(e.Station), lower(e.EMLabel)}] = emID
			for _, s := range e.Sequences {
				_, err := p.pool.Exec(ctx, `
				    INSERT INTO sequence (em_id, seq_index, name, is_production,
				                          cycle_start_step, cycle_complete_step)
				    VALUES ($1,$2,$3,$4,$5,$6)
				    ON CONFLICT (em_id, seq_index) DO UPDATE SET
				      name=$3, is_production=$4, cycle_start_step=$5,
				      cycle_complete_step=$6`,
					emID, s.Index, s.Name, s.IsProduction, s.CycleStart, s.CycleComplete)
				if err != nil {
					return nil, err
				}
			}
		}
	}
	return out, nil
}

type SeqConfig struct {
	Index                     int16
	Name                      string
	IsProduction              bool
	CycleStart, CycleComplete string
}

type EMConfig struct {
	Station, EMLabel, DisplayName string
	Enabled                       bool
	Sequences                     []SeqConfig
}

type LineConfig struct {
	Name, Host string
	Enabled    bool
	EMs        []EMConfig
}

func lower(s string) string {
	out := []byte(s)
	for i, c := range out {
		if c >= 'A' && c <= 'Z' {
			out[i] = c + 32
		}
	}
	return string(out)
}

// ── Store interface (non-blocking enqueue) ───────────────────────────────

func (p *PG) enqueue(r row) {
	select {
	case p.ch <- r:
	default:
		p.log.Error("write queue full — dropping row", "kind", string(r.kind))
	}
}

func (p *PG) AddStateInterval(v model.StateInterval) { p.enqueue(row{kind: 's', si: v}) }
func (p *PG) AddStepEvent(v model.StepEvent)         { p.enqueue(row{kind: 'e', se: v}) }
func (p *PG) AddCycle(v model.Cycle)                 { p.enqueue(row{kind: 'c', cy: v}) }
func (p *PG) AddModeInterval(v model.ModeInterval)   { p.enqueue(row{kind: 'm', mi: v}) }
func (p *PG) AddOperatorEvent(v model.OperatorEvent) { p.enqueue(row{kind: 'o', oe: v}) }

// Run is the writer loop; call in its own goroutine. Close ctx to flush
// and stop.
func (p *PG) Run(ctx context.Context) {
	defer close(p.done)
	ticker := time.NewTicker(flushInterval)
	defer ticker.Stop()
	buf := make([]row, 0, flushSize)
	flush := func() {
		if len(buf) == 0 {
			return
		}
		if err := p.flush(buf); err != nil {
			p.log.Error("flush failed", "rows", len(buf), "err", err)
		}
		buf = buf[:0]
	}
	for {
		select {
		case <-ctx.Done():
			// drain what's queued, then stop
			for {
				select {
				case r := <-p.ch:
					buf = append(buf, r)
				default:
					flush()
					return
				}
			}
		case r := <-p.ch:
			buf = append(buf, r)
			if len(buf) >= flushSize {
				flush()
			}
		case <-ticker.C:
			flush()
		}
	}
}

func (p *PG) Wait() { <-p.done }

func (p *PG) flush(rows []row) error {
	batch := &pgx.Batch{}
	for _, r := range rows {
		switch r.kind {
		case 's':
			batch.Queue(`INSERT INTO state_interval
			    (start_ts, em_id, end_ts, state, reason_type, reason,
			     seq_index, step_name, ack_ts)
			    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)`,
				r.si.StartTs, r.si.EMID, r.si.EndTs, r.si.State,
				r.si.ReasonType, r.si.Reason, r.si.SeqIndex, r.si.StepName, r.si.AckTs)
		case 'e':
			batch.Queue(`INSERT INTO step_event
			    (start_ts, em_id, end_ts, seq_index, step_name, step_desc,
			     duration_ms, was_faulted)
			    VALUES ($1,$2,$3,$4,$5,$6,$7,$8)`,
				r.se.StartTs, r.se.EMID, r.se.EndTs, r.se.SeqIndex,
				r.se.StepName, r.se.StepDesc, r.se.DurationMs, r.se.WasFaulted)
		case 'c':
			batch.Queue(`INSERT INTO cycle
			    (start_ts, em_id, end_ts, seq_index, work_end_ts, work_ms,
			     exchange_ms, total_ms, interrupted)
			    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)`,
				r.cy.StartTs, r.cy.EMID, r.cy.EndTs, r.cy.SeqIndex,
				r.cy.WorkEndTs, r.cy.WorkMs, r.cy.ExchangeMs, r.cy.TotalMs, r.cy.Interrupted)
		case 'm':
			batch.Queue(`INSERT INTO mode_interval (start_ts, em_id, end_ts, flag)
			    VALUES ($1,$2,$3,$4)`,
				r.mi.StartTs, r.mi.EMID, r.mi.EndTs, r.mi.Flag)
		case 'o':
			batch.Queue(`INSERT INTO operator_event (ts, em_id, event)
			    VALUES ($1,$2,$3)`, r.oe.Ts, r.oe.EMID, r.oe.Event)
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	return p.pool.SendBatch(ctx, batch).Close()
}

func (p *PG) Close() { p.pool.Close() }
