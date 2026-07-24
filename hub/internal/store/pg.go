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
	    display_name TEXT NOT NULL DEFAULT '',
	    enabled BOOL NOT NULL DEFAULT TRUE)`,
	`CREATE TABLE IF NOT EXISTS plc (
	    id SERIAL PRIMARY KEY,
	    name TEXT UNIQUE NOT NULL,
	    host TEXT NOT NULL DEFAULT '',
	    enabled BOOL NOT NULL DEFAULT TRUE)`,
	// station belongs to exactly one line and is hosted by one PLC; a PLC's
	// stations may span lines. Station names are unique WITHIN a line.
	`CREATE TABLE IF NOT EXISTS station (
	    id SERIAL PRIMARY KEY,
	    line_id INT NOT NULL REFERENCES line(id) ON DELETE CASCADE,
	    plc_id INT REFERENCES plc(id) ON DELETE SET NULL,
	    name TEXT NOT NULL,
	    display_name TEXT NOT NULL DEFAULT '',
	    enabled BOOL NOT NULL DEFAULT TRUE,
	    UNIQUE (line_id, name))`,
	`CREATE TABLE IF NOT EXISTS em (
	    id SERIAL PRIMARY KEY,
	    station_id INT NOT NULL REFERENCES station(id) ON DELETE CASCADE,
	    em_label TEXT NOT NULL,
	    display_name TEXT NOT NULL DEFAULT '',
	    enabled BOOL NOT NULL DEFAULT TRUE,
	    confirmed BOOL NOT NULL DEFAULT FALSE,
	    wire_version INT NOT NULL DEFAULT 0,
	    UNIQUE (station_id, em_label))`,
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
	`CREATE TABLE IF NOT EXISTS down_episode (
	    start_ts TIMESTAMPTZ NOT NULL,
	    em_id INT NOT NULL,
	    end_ts TIMESTAMPTZ NOT NULL,
	    reason_type TEXT NOT NULL DEFAULT '',
	    reason TEXT NOT NULL DEFAULT '',
	    seq_index SMALLINT,
	    step_name TEXT,
	    ack_ts TIMESTAMPTZ,
	    retries INT NOT NULL DEFAULT 0,
	    down_ms BIGINT NOT NULL DEFAULT 0)`,
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
	{"down_episode", "start_ts"},
	{"operator_event", "ts"},
}

var indexes = []string{
	`CREATE INDEX IF NOT EXISTS idx_station_line ON station (line_id)`,
	`CREATE INDEX IF NOT EXISTS idx_em_station ON em (station_id)`,
	`CREATE INDEX IF NOT EXISTS idx_state_em ON state_interval (em_id, start_ts DESC)`,
	`CREATE INDEX IF NOT EXISTS idx_step_em ON step_event (em_id, seq_index, start_ts DESC)`,
	`CREATE INDEX IF NOT EXISTS idx_cycle_em ON cycle (em_id, start_ts DESC)`,
	`CREATE INDEX IF NOT EXISTS idx_mode_em ON mode_interval (em_id, start_ts DESC)`,
	`CREATE INDEX IF NOT EXISTS idx_oper_em ON operator_event (em_id, ts DESC)`,
	`CREATE INDEX IF NOT EXISTS idx_episode_em ON down_episode (em_id, start_ts DESC)`,
}

type row struct {
	kind byte // s=state e=step c=cycle m=mode o=operator d=episode
	si   model.StateInterval
	se   model.StepEvent
	cy   model.Cycle
	mi   model.ModeInterval
	oe   model.OperatorEvent
	de   model.DownEpisode
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

// RegisterEM upserts the line -> station -> EM chain discovered live from a
// v4 datagram and returns the EM id. New EMs are enabled (tracked + visible)
// but UNCONFIRMED until an engineer vets them in the UI. Only wire_version is
// refreshed on an existing EM; nothing else is touched.
func (p *PG) RegisterEM(ctx context.Context, lineName, station, emLabel string, wireVer int) (int, error) {
	var lineID int
	if err := p.pool.QueryRow(ctx, `
	    INSERT INTO line (name) VALUES ($1)
	    ON CONFLICT (name) DO UPDATE SET name=EXCLUDED.name
	    RETURNING id`, lineName).Scan(&lineID); err != nil {
		return 0, err
	}
	var stationID int
	if err := p.pool.QueryRow(ctx, `
	    INSERT INTO station (line_id, name) VALUES ($1,$2)
	    ON CONFLICT (line_id, name) DO UPDATE SET name=EXCLUDED.name
	    RETURNING id`, lineID, station).Scan(&stationID); err != nil {
		return 0, err
	}
	var emID int
	err := p.pool.QueryRow(ctx, `
	    INSERT INTO em (station_id, em_label, enabled, confirmed, wire_version)
	    VALUES ($1,$2,TRUE,FALSE,$3)
	    ON CONFLICT (station_id, em_label)
	    DO UPDATE SET wire_version=EXCLUDED.wire_version
	    RETURNING id`, stationID, emLabel, wireVer).Scan(&emID)
	return emID, err
}

// EMRec / StationRec / LineRec describe the persisted hierarchy the collector
// rebuilds trackers and the query API from at startup and on refresh.
type EMRec struct {
	ID          int
	EMLabel     string
	DisplayName string
	Confirmed   bool
	Sequences   []SeqConfig
}

type StationRec struct {
	Name        string
	DisplayName string
	PLC         string
	EMs         []EMRec
}

type LineRec struct {
	Name        string
	DisplayName string
	Stations    []StationRec
}

// SeqConfigFor returns one EM's sequence config (for live tracker reload
// after a UI edit).
func (p *PG) SeqConfigFor(ctx context.Context, emID int) ([]SeqConfig, error) {
	rows, err := p.pool.Query(ctx, `
	    SELECT seq_index, name, is_production, cycle_start_step, cycle_complete_step
	    FROM sequence WHERE em_id = $1 ORDER BY seq_index`, emID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []SeqConfig
	for rows.Next() {
		var s SeqConfig
		if err := rows.Scan(&s.Index, &s.Name, &s.IsProduction,
			&s.CycleStart, &s.CycleComplete); err != nil {
			return nil, err
		}
		out = append(out, s)
	}
	return out, rows.Err()
}

// LoadHierarchy returns the enabled line -> station -> EM -> sequence tree
// from the DB — the sole source of truth (auto-discovered + UI edits).
func (p *PG) LoadHierarchy(ctx context.Context) ([]LineRec, error) {
	// sequences first, keyed by em id, so we attach them inline while building
	// the tree (no held pointers into growing slices).
	seqByEM := map[int][]SeqConfig{}
	seqRows, err := p.pool.Query(ctx, `
	    SELECT em_id, seq_index, name, is_production, cycle_start_step, cycle_complete_step
	    FROM sequence ORDER BY em_id, seq_index`)
	if err != nil {
		return nil, err
	}
	for seqRows.Next() {
		var emID int
		var s SeqConfig
		if err := seqRows.Scan(&emID, &s.Index, &s.Name, &s.IsProduction,
			&s.CycleStart, &s.CycleComplete); err != nil {
			seqRows.Close()
			return nil, err
		}
		seqByEM[emID] = append(seqByEM[emID], s)
	}
	seqRows.Close()
	if err := seqRows.Err(); err != nil {
		return nil, err
	}

	rows, err := p.pool.Query(ctx, `
	    SELECT l.name, l.display_name, s.name, s.display_name, COALESCE(p.name,''),
	           e.id, e.em_label, e.display_name, e.confirmed
	    FROM line l
	    JOIN station s ON s.line_id = l.id
	    JOIN em e ON e.station_id = s.id
	    LEFT JOIN plc p ON p.id = s.plc_id
	    WHERE l.enabled AND s.enabled AND e.enabled
	    ORDER BY l.name, s.name, e.em_label`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	lineOrder := []string{}
	lineByName := map[string]*LineRec{} // heap pointers stay stable
	stIdx := map[string]int{}           // "line\x00station" -> index in lr.Stations
	for rows.Next() {
		var lineName, lineDisp, stName, stDisp, plcName string
		var em EMRec
		if err := rows.Scan(&lineName, &lineDisp, &stName, &stDisp, &plcName,
			&em.ID, &em.EMLabel, &em.DisplayName, &em.Confirmed); err != nil {
			return nil, err
		}
		em.Sequences = seqByEM[em.ID]
		lr := lineByName[lineName]
		if lr == nil {
			lr = &LineRec{Name: lineName, DisplayName: lineDisp}
			lineByName[lineName] = lr
			lineOrder = append(lineOrder, lineName)
		}
		sk := lineName + "\x00" + stName
		idx, ok := stIdx[sk]
		if !ok {
			idx = len(lr.Stations)
			lr.Stations = append(lr.Stations, StationRec{Name: stName, DisplayName: stDisp, PLC: plcName})
			stIdx[sk] = idx
		}
		lr.Stations[idx].EMs = append(lr.Stations[idx].EMs, em)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	out := make([]LineRec, 0, len(lineOrder))
	for _, n := range lineOrder {
		out = append(out, *lineByName[n])
	}
	return out, nil
}

type SeqConfig struct {
	Index                     int16
	Name                      string
	IsProduction              bool
	CycleStart, CycleComplete string
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
func (p *PG) AddDownEpisode(v model.DownEpisode)     { p.enqueue(row{kind: 'd', de: v}) }

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
		case 'd':
			batch.Queue(`INSERT INTO down_episode
			    (start_ts, em_id, end_ts, reason_type, reason, seq_index,
			     step_name, ack_ts, retries, down_ms)
			    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)`,
				r.de.StartTs, r.de.EMID, r.de.EndTs, r.de.ReasonType,
				r.de.Reason, r.de.SeqIndex, r.de.StepName, r.de.AckTs,
				r.de.Retries, r.de.DownMs)
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	return p.pool.SendBatch(ctx, batch).Close()
}

func (p *PG) Close() { p.pool.Close() }

// Pool exposes the connection pool for the query API.
func (p *PG) Pool() *pgxpool.Pool { return p.pool }
