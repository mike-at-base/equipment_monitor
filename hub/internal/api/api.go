// Package api is the query surface of emhub. Design rule from the
// redesign doc: THE MATH LIVES HERE — the UI, the MCP tools, and emctl all
// read these endpoints, so humans and agents can never disagree on a
// number.
//
// Endpoints (all windows accept ?window=today|Nh|Nd or ?from=&to= RFC3339):
//
//	GET /api/v2/lines                          hierarchy + live rollup
//	GET /api/v2/lines/{line}/summary           full line summary
//	GET /api/v2/compare?a=LINE&b=LINE          decomposed delta of two lines
//	GET /api/v2/ems/{line}/{station}/{label}/intervals?state=
//	GET /api/v2/ems/{line}/{station}/{label}/steps?limit=
//	GET /api/v2/ems/{line}/{station}/{label}/cycles
//	GET /api/v2/ems/{line}/{station}/{label}/downs
//	GET /api/v2/ems/{line}/{station}/{label}/debug   raw telemetry + resets + modes
//	GET /api/v2/live                           current state of every EM
package api

import (
	"context"
	"encoding/json"
	"net/http"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/mike-at-base/equipment_monitor/hub/internal/ingest"
	"github.com/mike-at-base/equipment_monitor/hub/internal/model"
)

type EMInfo struct {
	ID        int    `json:"-"`
	Station   string `json:"station"` // owning station name (handler convenience)
	Label     string `json:"em_label"`
	Display   string `json:"display_name"`
	Confirmed bool   `json:"confirmed"`
}

type StationInfo struct {
	Name    string   `json:"name"`
	Display string   `json:"display_name"`
	PLC     string   `json:"plc"`
	EMs     []EMInfo `json:"ems"`
}

type LineInfo struct {
	Name     string        `json:"name"`
	Display  string        `json:"display_name"`
	Stations []StationInfo `json:"stations"`
}

// EMs flattens a line's EMs across its stations (for summary aggregation).
func (l *LineInfo) EMs() []EMInfo {
	var out []EMInfo
	for i := range l.Stations {
		out = append(out, l.Stations[i].EMs...)
	}
	return out
}

type Server struct {
	pool     *pgxpool.Pool
	linesMu  sync.RWMutex
	lines    []LineInfo
	live     func() []ingest.LiveEM
	raw      func() []ingest.RawEM
	onConfig func(emID int) // reload tracker + hierarchy after a config save
	onDelete func(emID int) // remove tracker + rows + refresh after a delete
	tz       *time.Location
}

// SetLines swaps the hierarchy (called by the background refresh so
// auto-discovered EMs appear without a restart).
func (s *Server) SetLines(lines []LineInfo) {
	s.linesMu.Lock()
	s.lines = lines
	s.linesMu.Unlock()
}

func (s *Server) snapshotLines() []LineInfo {
	s.linesMu.RLock()
	defer s.linesMu.RUnlock()
	return s.lines
}

func New(pool *pgxpool.Pool, lines []LineInfo,
	live func() []ingest.LiveEM, raw func() []ingest.RawEM,
	onConfig func(emID int), onDelete func(emID int)) *Server {
	tz, err := time.LoadLocation(envOr("APP_TIMEZONE", "America/Chicago"))
	if err != nil {
		tz = time.UTC
	}
	return &Server{pool: pool, lines: lines, live: live, raw: raw,
		onConfig: onConfig, onDelete: onDelete, tz: tz}
}

func envOr(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

func (s *Server) Register(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/v2/lines", s.handleLines)
	mux.HandleFunc("GET /api/v2/lines/{line}/summary", s.handleLineSummary)
	mux.HandleFunc("GET /api/v2/compare", s.handleCompare)
	mux.HandleFunc("GET /api/v2/ems/{line}/{station}/{label}/intervals", s.handleIntervals)
	mux.HandleFunc("GET /api/v2/ems/{line}/{station}/{label}/steps", s.handleSteps)
	mux.HandleFunc("GET /api/v2/ems/{line}/{station}/{label}/cycles", s.handleCycles)
	mux.HandleFunc("GET /api/v2/ems/{line}/{station}/{label}/throughput", s.handleThroughput)
	mux.HandleFunc("GET /api/v2/ems/{line}/{station}/{label}/downs", s.handleDowns)
	mux.HandleFunc("GET /api/v2/ems/{line}/{station}/{label}/debug", s.handleDebug)
	mux.HandleFunc("GET /api/v2/ems/{line}/{station}/{label}/config", s.handleGetConfig)
	mux.HandleFunc("PUT /api/v2/ems/{line}/{station}/{label}/config", s.handleSaveConfig)
	mux.HandleFunc("DELETE /api/v2/ems/{line}/{station}/{label}", s.handleDeleteEM)
	mux.HandleFunc("GET /api/v2/unconfirmed", s.handleUnconfirmed)
	mux.HandleFunc("GET /api/v2/hierarchy", s.handleHierarchy)
}

// handleHierarchy returns the full line -> station -> em tree (with display
// names + confirmed flags) for the SCADA navigation.
func (s *Server) handleHierarchy(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, s.snapshotLines())
}

// ── helpers ──────────────────────────────────────────────────────────────

func (s *Server) window(r *http.Request) (time.Time, time.Time, error) {
	q := r.URL.Query()
	now := time.Now().UTC()
	if f, t := q.Get("from"), q.Get("to"); f != "" {
		from, err := time.Parse(time.RFC3339, f)
		if err != nil {
			return time.Time{}, time.Time{}, err
		}
		to := now
		if t != "" {
			if to, err = time.Parse(time.RFC3339, t); err != nil {
				return time.Time{}, time.Time{}, err
			}
		}
		return from.UTC(), to.UTC(), nil
	}
	w := q.Get("window")
	switch {
	case w == "" || w == "today":
		local := now.In(s.tz)
		midnight := time.Date(local.Year(), local.Month(), local.Day(), 0, 0, 0, 0, s.tz)
		return midnight.UTC(), now, nil
	case strings.HasSuffix(w, "h") || strings.HasSuffix(w, "m") || strings.HasSuffix(w, "d"):
		if strings.HasSuffix(w, "d") {
			n, err := strconv.Atoi(strings.TrimSuffix(w, "d"))
			if err != nil {
				return time.Time{}, time.Time{}, err
			}
			return now.Add(-time.Duration(n) * 24 * time.Hour), now, nil
		}
		d, err := time.ParseDuration(w)
		if err != nil {
			return time.Time{}, time.Time{}, err
		}
		return now.Add(-d), now, nil
	}
	return time.Time{}, time.Time{}, errBadWindow
}

var errBadWindow = jsonErr("invalid window: use today, 8h, 3d, or from/to RFC3339")

type jsonErr string

func (e jsonErr) Error() string { return string(e) }

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func httpErr(w http.ResponseWriter, code int, err error) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(map[string]string{"error": err.Error()})
}

func (s *Server) findLine(name string) *LineInfo {
	lines := s.snapshotLines()
	for i := range lines {
		if strings.EqualFold(lines[i].Name, name) {
			return &lines[i]
		}
	}
	return nil
}

func (s *Server) findEM(line, station, label string) (*LineInfo, *EMInfo) {
	l := s.findLine(line)
	if l == nil {
		return nil, nil
	}
	for si := range l.Stations {
		if !strings.EqualFold(l.Stations[si].Name, station) {
			continue
		}
		for ei := range l.Stations[si].EMs {
			if strings.EqualFold(l.Stations[si].EMs[ei].Label, label) {
				return l, &l.Stations[si].EMs[ei]
			}
		}
	}
	return l, nil
}

func overlapMs(aStart, aEnd, wStart, wEnd time.Time) int64 {
	st, en := aStart, aEnd
	if st.Before(wStart) {
		st = wStart
	}
	if en.After(wEnd) {
		en = wEnd
	}
	if !en.After(st) {
		return 0
	}
	return en.Sub(st).Milliseconds()
}

// openContribution folds the in-memory open intervals into per-EM state
// minutes so "today" includes the state the machine is in right now.
func (s *Server) openContribution(emIDs map[int]bool, from, to time.Time,
	stateMs map[int]map[string]int64) {
	for _, le := range s.live() {
		if !emIDs[le.EMID] || le.State == "" || le.Since.IsZero() {
			continue
		}
		ms := overlapMs(le.Since, time.Now().UTC(), from, to)
		if ms <= 0 {
			continue
		}
		if stateMs[le.EMID] == nil {
			stateMs[le.EMID] = map[string]int64{}
		}
		stateMs[le.EMID][le.State] += ms
	}
}

// ── aggregation ──────────────────────────────────────────────────────────

const availStates = "productive standby starved blocked process_wait wait paused"

// availability = available / (available + down); manual and offline are
// excluded from the denominator (non-scheduled / no-data time).
func availability(ms map[string]int64) (pct float64, ok bool) {
	var avail, down int64
	for st, v := range ms {
		if strings.Contains(availStates, st) {
			avail += v
		}
		if st == model.StateDown {
			down += v
		}
	}
	if avail+down == 0 {
		return 0, false
	}
	return 100 * float64(avail) / float64(avail+down), true
}

func (s *Server) stateMinutes(ctx context.Context, ids []int, from, to time.Time) (map[int]map[string]int64, error) {
	rows, err := s.pool.Query(ctx, `
	    SELECT em_id, state,
	           SUM(EXTRACT(EPOCH FROM (LEAST(end_ts,$3) - GREATEST(start_ts,$2)))*1000)::bigint
	    FROM state_interval
	    WHERE em_id = ANY($1) AND end_ts > $2 AND start_ts < $3
	    GROUP BY em_id, state`, ids, from, to)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[int]map[string]int64{}
	for rows.Next() {
		var id int
		var st string
		var ms int64
		if err := rows.Scan(&id, &st, &ms); err != nil {
			return nil, err
		}
		if out[id] == nil {
			out[id] = map[string]int64{}
		}
		out[id][st] += ms
	}
	return out, rows.Err()
}

type DownRow struct {
	Station    string     `json:"station"`
	EMLabel    string     `json:"em_label"`
	StartTs    time.Time  `json:"start_ts"`
	EndTs      time.Time  `json:"end_ts"`
	Minutes    float64    `json:"minutes"`
	ReasonType string     `json:"reason_type"`
	Reason     string     `json:"reason"`
	StepName   string     `json:"step_name,omitempty"`
	AckTs      *time.Time `json:"ack_ts,omitempty"`
	RespMin    *float64   `json:"response_min,omitempty"`
	RepairMin  *float64   `json:"repair_min,omitempty"`
}

func (s *Server) downRows(ctx context.Context, ids []int, byID map[int]EMInfo,
	from, to time.Time, limit int) ([]DownRow, error) {
	rows, err := s.pool.Query(ctx, `
	    SELECT em_id, start_ts, end_ts, reason_type, reason,
	           COALESCE(step_name,''), ack_ts
	    FROM state_interval
	    WHERE em_id = ANY($1) AND state='down' AND end_ts > $2 AND start_ts < $3
	    ORDER BY start_ts DESC LIMIT $4`, ids, from, to, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []DownRow{}
	for rows.Next() {
		var id int
		var d DownRow
		if err := rows.Scan(&id, &d.StartTs, &d.EndTs, &d.ReasonType,
			&d.Reason, &d.StepName, &d.AckTs); err != nil {
			return nil, err
		}
		info := byID[id]
		d.Station, d.EMLabel = info.Station, info.Label
		d.Minutes = round1(d.EndTs.Sub(d.StartTs).Minutes())
		if d.AckTs != nil {
			r := round1(d.AckTs.Sub(d.StartTs).Minutes())
			p := round1(d.EndTs.Sub(*d.AckTs).Minutes())
			d.RespMin, d.RepairMin = &r, &p
		}
		out = append(out, d)
	}
	return out, rows.Err()
}

type ReasonAgg struct {
	Reason     string  `json:"reason"`
	ReasonType string  `json:"reason_type"`
	Count      int     `json:"count"`
	Minutes    float64 `json:"minutes"`
}

func topReasons(downs []DownRow, n int) []ReasonAgg {
	agg := map[string]*ReasonAgg{}
	for _, d := range downs {
		key := d.ReasonType + "|" + d.Reason
		if agg[key] == nil {
			agg[key] = &ReasonAgg{Reason: d.Reason, ReasonType: d.ReasonType}
		}
		agg[key].Count++
		agg[key].Minutes = round1(agg[key].Minutes + d.Minutes)
	}
	out := make([]ReasonAgg, 0, len(agg))
	for _, a := range agg {
		out = append(out, *a)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Minutes > out[j].Minutes })
	if len(out) > n {
		out = out[:n]
	}
	return out
}

type CycleStats struct {
	Count       int      `json:"count"`
	Interrupted int      `json:"interrupted"`
	AvgMs       *float64 `json:"avg_ms,omitempty"`
	P50Ms       *float64 `json:"p50_ms,omitempty"`
	P90Ms       *float64 `json:"p90_ms,omitempty"`
	WorkAvgMs   *float64 `json:"work_avg_ms,omitempty"`
	ExchAvgMs   *float64 `json:"exchange_avg_ms,omitempty"`
	PerHour     *float64 `json:"per_hour,omitempty"`
}

func (s *Server) cycleStats(ctx context.Context, ids []int, from, to time.Time) (CycleStats, error) {
	var cs CycleStats
	err := s.pool.QueryRow(ctx, `
	    SELECT count(*),
	           count(*) FILTER (WHERE interrupted),
	           avg(total_ms),
	           percentile_cont(0.5) WITHIN GROUP (ORDER BY total_ms),
	           percentile_cont(0.9) WITHIN GROUP (ORDER BY total_ms),
	           avg(work_ms), avg(exchange_ms)
	    FROM cycle
	    WHERE em_id = ANY($1) AND start_ts >= $2 AND start_ts < $3`,
		ids, from, to).Scan(&cs.Count, &cs.Interrupted, &cs.AvgMs,
		&cs.P50Ms, &cs.P90Ms, &cs.WorkAvgMs, &cs.ExchAvgMs)
	if err != nil {
		return cs, err
	}
	hours := to.Sub(from).Hours()
	if hours > 0 && cs.Count > 0 {
		ph := round1(float64(cs.Count) / hours)
		cs.PerHour = &ph
	}
	for _, p := range []**float64{&cs.AvgMs, &cs.P50Ms, &cs.P90Ms, &cs.WorkAvgMs, &cs.ExchAvgMs} {
		if *p != nil {
			v := round1(**p)
			*p = &v
		}
	}
	return cs, nil
}

type ThroughputBucket struct {
	BucketTs time.Time `json:"bucket_ts"`
	Count    int       `json:"count"`
}

// bucketDurations are the toggle options the throughput chart offers.
var bucketDurations = map[string]time.Duration{
	"15m": 15 * time.Minute,
	"30m": 30 * time.Minute,
	"1h":  time.Hour,
}

func parseBucket(s string) (string, time.Duration) {
	if d, ok := bucketDurations[s]; ok {
		return s, d
	}
	return "1h", time.Hour // default: per hour
}

// cycleThroughput counts completed cycles per fixed-width time bucket.
func (s *Server) cycleThroughput(ctx context.Context, ids []int, from, to time.Time,
	bucket time.Duration) ([]ThroughputBucket, error) {
	rows, err := s.pool.Query(ctx, `
	    SELECT time_bucket(make_interval(secs => $4), start_ts) AS b, count(*)
	    FROM cycle
	    WHERE em_id = ANY($1) AND start_ts >= $2 AND start_ts < $3
	    GROUP BY b ORDER BY b`, ids, from, to, bucket.Seconds())
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []ThroughputBucket{}
	for rows.Next() {
		var b ThroughputBucket
		if err := rows.Scan(&b.BucketTs, &b.Count); err != nil {
			return nil, err
		}
		out = append(out, b)
	}
	return out, rows.Err()
}

func (s *Server) modeMinutes(ctx context.Context, ids []int, from, to time.Time) (map[string]float64, error) {
	rows, err := s.pool.Query(ctx, `
	    SELECT flag,
	           SUM(EXTRACT(EPOCH FROM (LEAST(end_ts,$3) - GREATEST(start_ts,$2)))/60.0)
	    FROM mode_interval
	    WHERE em_id = ANY($1) AND end_ts > $2 AND start_ts < $3
	    GROUP BY flag`, ids, from, to)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]float64{}
	for rows.Next() {
		var f string
		var m float64
		if err := rows.Scan(&f, &m); err != nil {
			return nil, err
		}
		out[f] = round1(m)
	}
	return out, rows.Err()
}

type FlowAgg struct {
	Station string  `json:"station"`
	EMLabel string  `json:"em_label"`
	State   string  `json:"state"`
	Minutes float64 `json:"minutes"`
	Count   int     `json:"count"`
	Reason  string  `json:"top_reason"`
}

func (s *Server) flowLosses(ctx context.Context, ids []int, byID map[int]EMInfo,
	from, to time.Time) ([]FlowAgg, error) {
	rows, err := s.pool.Query(ctx, `
	    SELECT em_id, state,
	           SUM(EXTRACT(EPOCH FROM (LEAST(end_ts,$3) - GREATEST(start_ts,$2)))/60.0),
	           count(*),
	           (array_agg(reason ORDER BY end_ts-start_ts DESC))[1]
	    FROM state_interval
	    WHERE em_id = ANY($1) AND state IN ('starved','blocked','process_wait','wait')
	      AND end_ts > $2 AND start_ts < $3
	    GROUP BY em_id, state`, ids, from, to)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []FlowAgg{}
	for rows.Next() {
		var id, n int
		var st, reason string
		var m float64
		if err := rows.Scan(&id, &st, &m, &n, &reason); err != nil {
			return nil, err
		}
		info := byID[id]
		out = append(out, FlowAgg{Station: info.Station, EMLabel: info.Label,
			State: st, Minutes: round1(m), Count: n, Reason: reason})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Minutes > out[j].Minutes })
	return out, rows.Err()
}

func round1(f float64) float64 { return float64(int64(f*10+0.5)) / 10 }

// ── down episodes (sticky root cause — the reporting layer) ─────────────

type EpisodeRow struct {
	Station    string     `json:"station"`
	EMLabel    string     `json:"em_label"`
	StartTs    time.Time  `json:"start_ts"`
	EndTs      time.Time  `json:"end_ts"`
	Ongoing    bool       `json:"ongoing,omitempty"`
	Minutes    float64    `json:"minutes"`
	ReasonType string     `json:"reason_type"`
	Reason     string     `json:"reason"`
	StepName   string     `json:"step_name,omitempty"`
	Retries    int        `json:"retries"`
	DownMin    float64    `json:"raw_down_min"`
	AckTs      *time.Time `json:"ack_ts,omitempty"`
	RespMin    *float64   `json:"response_min,omitempty"`
	RepairMin  *float64   `json:"repair_min,omitempty"`
}

// episodeRows returns closed episodes from the DB plus any open episode
// from the live trackers, window-clipped for minute math.
func (s *Server) episodeRows(ctx context.Context, ids []int, byID map[int]EMInfo,
	from, to time.Time, limit int) ([]EpisodeRow, error) {
	rows, err := s.pool.Query(ctx, `
	    SELECT em_id, start_ts, end_ts, reason_type, reason,
	           COALESCE(step_name,''), retries, down_ms, ack_ts
	    FROM down_episode
	    WHERE em_id = ANY($1) AND end_ts > $2 AND start_ts < $3
	    ORDER BY start_ts DESC LIMIT $4`, ids, from, to, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	idSet := map[int]bool{}
	for _, id := range ids {
		idSet[id] = true
	}
	out := []EpisodeRow{}
	for rows.Next() {
		var id, retries int
		var downMs int64
		var e EpisodeRow
		if err := rows.Scan(&id, &e.StartTs, &e.EndTs, &e.ReasonType,
			&e.Reason, &e.StepName, &retries, &downMs, &e.AckTs); err != nil {
			return nil, err
		}
		info := byID[id]
		e.Station, e.EMLabel, e.Retries = info.Station, info.Label, retries
		e.Minutes = round1(float64(overlapMs(e.StartTs, e.EndTs, from, to)) / 60000.0)
		e.DownMin = round1(float64(downMs) / 60000.0)
		if e.AckTs != nil {
			r := round1(e.AckTs.Sub(e.StartTs).Minutes())
			p := round1(e.EndTs.Sub(*e.AckTs).Minutes())
			e.RespMin, e.RepairMin = &r, &p
		}
		out = append(out, e)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	// open episodes from live trackers
	now := time.Now().UTC()
	for _, le := range s.live() {
		if !le.EpisodeOpen || !idSet[le.EMID] || !le.EpisodeStart.Before(to) {
			continue
		}
		e := EpisodeRow{
			Station: le.Station, EMLabel: le.EMLabel,
			StartTs: le.EpisodeStart, EndTs: now, Ongoing: true,
			ReasonType: le.EpisodeRType, Reason: le.EpisodeReason,
			StepName: le.EpisodeStep, Retries: le.EpisodeRetries,
		}
		e.Minutes = round1(float64(overlapMs(e.StartTs, now, from, to)) / 60000.0)
		out = append([]EpisodeRow{e}, out...)
	}
	return out, nil
}

func topEpisodeReasons(eps []EpisodeRow, n int) []ReasonAgg {
	agg := map[string]*ReasonAgg{}
	for _, e := range eps {
		key := e.ReasonType + "|" + e.Reason
		if agg[key] == nil {
			agg[key] = &ReasonAgg{Reason: e.Reason, ReasonType: e.ReasonType}
		}
		agg[key].Count++
		agg[key].Minutes = round1(agg[key].Minutes + e.Minutes)
	}
	out := make([]ReasonAgg, 0, len(agg))
	for _, a := range agg {
		out = append(out, *a)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Minutes > out[j].Minutes })
	if len(out) > n {
		out = out[:n]
	}
	return out
}

// episodeAvailability: unavailable time = episode spans (inter-states and
// retry blips inside an episode are NOT uptime).  availRaw already counts
// in-episode productive/paused blips as available and raw down time as
// not — correct by swapping raw down for episode span.
func episodeAvailability(availRawMs, rawDownMs, episodeMs int64) (float64, bool) {
	blips := episodeMs - rawDownMs // in-episode time raw math called available
	if blips < 0 {
		blips = 0
	}
	avail := availRawMs - blips
	if avail < 0 {
		avail = 0
	}
	if avail+episodeMs == 0 {
		return 0, false
	}
	return 100 * float64(avail) / float64(avail+episodeMs), true
}
