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
//	GET /api/v2/ems/{line}/{station}/{label}/steps?limit=&offset=
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

	"github.com/mike-at-base/equipment_monitor/hub/internal/compose"
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
	// cross-line: EMs are named LINE/STATION/label, so no {line} in the path
	mux.HandleFunc("GET /api/v2/emcompare", s.handleEMCompare)
	mux.HandleFunc("GET /api/v2/nodecompare", s.handleNodeCompare)
	mux.HandleFunc("GET /api/v2/ems/{line}/{station}/{label}/intervals", s.handleIntervals)
	mux.HandleFunc("GET /api/v2/ems/{line}/{station}/{label}/steps", s.handleSteps)
	mux.HandleFunc("GET /api/v2/ems/{line}/{station}/{label}/stepstats", s.handleStepStats)
	mux.HandleFunc("GET /api/v2/ems/{line}/{station}/{label}/stepdetail", s.handleStepDetail)
	mux.HandleFunc("GET /api/v2/ems/{line}/{station}/{label}/cycles", s.handleCycles)
	mux.HandleFunc("GET /api/v2/ems/{line}/{station}/{label}/cycledetail", s.handleCycleDetail)
	mux.HandleFunc("GET /api/v2/ems/{line}/{station}/{label}/throughput", s.handleThroughput)
	mux.HandleFunc("GET /api/v2/ems/{line}/{station}/{label}/downs", s.handleDowns)
	mux.HandleFunc("GET /api/v2/ems/{line}/{station}/{label}/debug", s.handleDebug)
	mux.HandleFunc("GET /api/v2/ems/{line}/{station}/{label}/config", s.handleGetConfig)
	mux.HandleFunc("PUT /api/v2/ems/{line}/{station}/{label}/config", s.handleSaveConfig)
	mux.HandleFunc("DELETE /api/v2/ems/{line}/{station}/{label}", s.handleDeleteEM)
	mux.HandleFunc("GET /api/v2/unconfirmed", s.handleUnconfirmed)
	mux.HandleFunc("GET /api/v2/hierarchy", s.handleHierarchy)
	mux.HandleFunc("GET /api/v2/lines/{line}/schedule", s.handleGetSchedule)
	mux.HandleFunc("PUT /api/v2/lines/{line}/schedule", s.handleSaveSchedule)
	mux.HandleFunc("GET /api/v2/lines/{line}/availmodel", s.handleGetLineModel)
	mux.HandleFunc("PUT /api/v2/lines/{line}/availmodel", s.handleSaveLineModel)
	mux.HandleFunc("GET /api/v2/lines/{line}/stations/{station}/availmodel", s.handleGetStationModel)
	mux.HandleFunc("PUT /api/v2/lines/{line}/stations/{station}/availmodel", s.handleSaveStationModel)
	mux.HandleFunc("GET /api/v2/lines/{line}/composed", s.handleLineComposed)
	mux.HandleFunc("GET /api/v2/lines/{line}/stations/{station}/composed", s.handleStationComposed)
	mux.HandleFunc("GET /api/v2/dashboards", s.handleListDashboards)
	mux.HandleFunc("GET /api/v2/dashboards/{slug}", s.handleGetDashboard)
	mux.HandleFunc("PUT /api/v2/dashboards/{slug}", s.handleSaveDashboard)
	mux.HandleFunc("DELETE /api/v2/dashboards/{slug}", s.handleDeleteDashboard)
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
	case w == "prod":
		// today's production span for this line: first shift start -> now
		// (clamped to the last shift end). Unscheduled lines fall back to today.
		local := now.In(s.tz)
		midnight := time.Date(local.Year(), local.Month(), local.Day(), 0, 0, 0, 0, s.tz).UTC()
		ranges, err := s.lineProductionRanges(r.Context(), r.PathValue("line"), midnight, now)
		if err != nil {
			return time.Time{}, time.Time{}, err
		}
		if len(ranges) == 0 {
			return now, now, nil // no production scheduled yet today
		}
		return ranges[0][0], ranges[len(ranges)-1][1], nil
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

// "nva" (non-value-added) counts as AVAILABLE: a purge does not make the
// tool unavailable, it is uptime that simply is not adding value. Note
// this is a lean concept, not a SEMI E10 state — E10 would classify most
// of these as Scheduled Downtime and charge them against availability.
// Chosen deliberately so tagging purge steps cannot move the availability
// number; the time shows up as its own state instead.
const availStates = "productive standby starved blocked process_wait wait paused nva"

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

// StepStat is the duration distribution of one step over the window —
// computed server-side across EVERY execution, not the page the UI happens
// to be showing. Box plot: box p25..p75, median p50, whiskers p05..p95;
// min/max/avg carried separately because a step timeout shows up as the
// max long before it moves a percentile.
type StepStat struct {
	SeqIndex int16   `json:"seq_index"`
	Step     string  `json:"step"`
	Desc     string  `json:"description"`
	Count    int     `json:"count"`
	Faulted  int     `json:"faulted"`
	MinMs    float64 `json:"min_ms"`
	P05Ms    float64 `json:"p05_ms"`
	P25Ms    float64 `json:"p25_ms"`
	P50Ms    float64 `json:"p50_ms"`
	P75Ms    float64 `json:"p75_ms"`
	P95Ms    float64 `json:"p95_ms"`
	MaxMs    float64 `json:"max_ms"`
	AvgMs    float64 `json:"avg_ms"`
}

func (s *Server) stepStats(ctx context.Context, emID int, from, to time.Time) ([]StepStat, error) {
	rows, err := s.pool.Query(ctx, `
	    SELECT seq_index, step_name,
	           COALESCE((array_agg(step_desc ORDER BY start_ts DESC))[1], ''),
	           count(*)::int,
	           count(*) FILTER (WHERE was_faulted)::int,
	           min(duration_ms)::float8,
	           percentile_cont(0.05) WITHIN GROUP (ORDER BY duration_ms),
	           percentile_cont(0.25) WITHIN GROUP (ORDER BY duration_ms),
	           percentile_cont(0.50) WITHIN GROUP (ORDER BY duration_ms),
	           percentile_cont(0.75) WITHIN GROUP (ORDER BY duration_ms),
	           percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms),
	           max(duration_ms)::float8,
	           avg(duration_ms)::float8
	    FROM step_event
	    WHERE em_id=$1 AND start_ts >= $2 AND start_ts < $3
	    GROUP BY seq_index, step_name
	    ORDER BY percentile_cont(0.50) WITHIN GROUP (ORDER BY duration_ms) DESC`,
		emID, from, to)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []StepStat{}
	for rows.Next() {
		var v StepStat
		if err := rows.Scan(&v.SeqIndex, &v.Step, &v.Desc, &v.Count, &v.Faulted,
			&v.MinMs, &v.P05Ms, &v.P25Ms, &v.P50Ms, &v.P75Ms, &v.P95Ms,
			&v.MaxMs, &v.AvgMs); err != nil {
			return nil, err
		}
		out = append(out, v)
	}
	return out, rows.Err()
}

// DurationHistogram is the duration distribution SHAPE of one step: counts in
// equal-width bins across [lo,hi]. The domain stops at p95 and everything
// past it lands in Overflow, otherwise a single multi-minute execution puts
// every real observation in bin 0.
type DurationHistogram struct {
	LoMs     float64 `json:"lo_ms"`
	HiMs     float64 `json:"hi_ms"`
	BinMs    float64 `json:"bin_ms"`
	Bins     []int   `json:"bins"`
	Overflow int     `json:"overflow"` // executions slower than hi
}

// DriftPoint is one time bucket of a step's duration distribution, so
// you can see the spread move rather than just its total.
type DriftPoint struct {
	BucketTs time.Time `json:"bucket_ts"`
	Count    int       `json:"count"`
	P25Ms    float64   `json:"p25_ms"`
	P50Ms    float64   `json:"p50_ms"`
	P75Ms    float64   `json:"p75_ms"`
	P95Ms    float64   `json:"p95_ms"`
}

const stepHistogramBins = 24

func (s *Server) stepHistogram(ctx context.Context, emID int, seq int16, step string,
	from, to time.Time) (DurationHistogram, error) {
	h := DurationHistogram{Bins: make([]int, stepHistogramBins)}
	var lo, hi *float64
	err := s.pool.QueryRow(ctx, `
	    SELECT min(duration_ms)::float8,
	           percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)
	    FROM step_event
	    WHERE em_id=$1 AND seq_index=$2 AND step_name=$3
	      AND start_ts >= $4 AND start_ts < $5`,
		emID, seq, step, from, to).Scan(&lo, &hi)
	if err != nil || lo == nil || hi == nil {
		return h, err
	}
	// a step with no variation still needs a non-zero domain
	if *hi <= *lo {
		*hi = *lo + 1
	}
	h.LoMs, h.HiMs = *lo, *hi
	h.BinMs = (*hi - *lo) / stepHistogramBins

	rows, err := s.pool.Query(ctx, `
	    SELECT width_bucket(duration_ms::float8, $6, $7, $8) AS bin, count(*)::int
	    FROM step_event
	    WHERE em_id=$1 AND seq_index=$2 AND step_name=$3
	      AND start_ts >= $4 AND start_ts < $5
	    GROUP BY bin ORDER BY bin`,
		emID, seq, step, from, to, h.LoMs, h.HiMs, stepHistogramBins)
	if err != nil {
		return h, err
	}
	defer rows.Close()
	for rows.Next() {
		var bin, n int
		if err := rows.Scan(&bin, &n); err != nil {
			return h, err
		}
		switch {
		case bin > stepHistogramBins: // above hi
			h.Overflow += n
		case bin < 1: // below lo (cannot happen, lo is the min) — fold into bin 0
			h.Bins[0] += n
		default:
			h.Bins[bin-1] += n
		}
	}
	return h, rows.Err()
}

func (s *Server) stepDrift(ctx context.Context, emID int, seq int16, step string,
	from, to time.Time, bucket time.Duration) ([]DriftPoint, error) {
	rows, err := s.pool.Query(ctx, `
	    SELECT to_timestamp(floor(extract(epoch FROM start_ts) / $6) * $6) AS bucket_ts,
	           count(*)::int,
	           percentile_cont(0.25) WITHIN GROUP (ORDER BY duration_ms),
	           percentile_cont(0.50) WITHIN GROUP (ORDER BY duration_ms),
	           percentile_cont(0.75) WITHIN GROUP (ORDER BY duration_ms),
	           percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)
	    FROM step_event
	    WHERE em_id=$1 AND seq_index=$2 AND step_name=$3
	      AND start_ts >= $4 AND start_ts < $5
	    GROUP BY 1 ORDER BY 1`,
		emID, seq, step, from, to, int(bucket.Seconds()))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []DriftPoint{}
	for rows.Next() {
		var p DriftPoint
		if err := rows.Scan(&p.BucketTs, &p.Count, &p.P25Ms, &p.P50Ms,
			&p.P75Ms, &p.P95Ms); err != nil {
			return nil, err
		}
		p.BucketTs = p.BucketTs.UTC()
		out = append(out, p)
	}
	return out, rows.Err()
}

// cycleMetrics whitelists the columns the cycle charts may aggregate. The
// metric name reaches SQL as an identifier, so it must never come straight
// from the query string.
var cycleMetrics = map[string]string{
	"total":    "total_ms",
	"work":     "work_ms",
	"exchange": "exchange_ms",
}

// CycleSpread is one metric's distribution across the window, for the
// total-vs-work-vs-exchange comparison.
type CycleSpread struct {
	Name  string  `json:"name"`
	Count int     `json:"count"`
	MinMs float64 `json:"min_ms"`
	P05Ms float64 `json:"p05_ms"`
	P25Ms float64 `json:"p25_ms"`
	P50Ms float64 `json:"p50_ms"`
	P75Ms float64 `json:"p75_ms"`
	P95Ms float64 `json:"p95_ms"`
	MaxMs float64 `json:"max_ms"`
}

func (s *Server) cycleSpread(ctx context.Context, emID int, from, to time.Time) ([]CycleSpread, error) {
	out := []CycleSpread{}
	for _, name := range []string{"total", "work", "exchange"} {
		col := cycleMetrics[name]
		var v CycleSpread
		v.Name = name
		var mn, p05, p25, p50, p75, p95, mx *float64
		err := s.pool.QueryRow(ctx, `
		    SELECT count(*)::int, min(`+col+`)::float8,
		           percentile_cont(0.05) WITHIN GROUP (ORDER BY `+col+`),
		           percentile_cont(0.25) WITHIN GROUP (ORDER BY `+col+`),
		           percentile_cont(0.50) WITHIN GROUP (ORDER BY `+col+`),
		           percentile_cont(0.75) WITHIN GROUP (ORDER BY `+col+`),
		           percentile_cont(0.95) WITHIN GROUP (ORDER BY `+col+`),
		           max(`+col+`)::float8
		    FROM cycle
		    WHERE em_id=$1 AND start_ts >= $2 AND start_ts < $3
		      AND `+col+` IS NOT NULL`, emID, from, to).
			Scan(&v.Count, &mn, &p05, &p25, &p50, &p75, &p95, &mx)
		if err != nil {
			return nil, err
		}
		if v.Count == 0 || mn == nil {
			continue // work/exchange are null until a cycle-complete step is set
		}
		v.MinMs, v.P05Ms, v.P25Ms = *mn, *p05, *p25
		v.P50Ms, v.P75Ms, v.P95Ms, v.MaxMs = *p50, *p75, *p95, *mx
		out = append(out, v)
	}
	return out, nil
}

func (s *Server) cycleHistogram(ctx context.Context, emID int, from, to time.Time,
	metric string) (DurationHistogram, error) {
	col := cycleMetrics[metric]
	h := DurationHistogram{Bins: make([]int, stepHistogramBins)}
	var lo, hi *float64
	err := s.pool.QueryRow(ctx, `
	    SELECT min(`+col+`)::float8,
	           percentile_cont(0.95) WITHIN GROUP (ORDER BY `+col+`)
	    FROM cycle WHERE em_id=$1 AND start_ts >= $2 AND start_ts < $3
	      AND `+col+` IS NOT NULL`, emID, from, to).Scan(&lo, &hi)
	if err != nil || lo == nil || hi == nil {
		return h, err
	}
	if *hi <= *lo {
		*hi = *lo + 1
	}
	h.LoMs, h.HiMs = *lo, *hi
	h.BinMs = (*hi - *lo) / stepHistogramBins

	rows, err := s.pool.Query(ctx, `
	    SELECT width_bucket(`+col+`::float8, $4, $5, $6) AS bin, count(*)::int
	    FROM cycle WHERE em_id=$1 AND start_ts >= $2 AND start_ts < $3
	      AND `+col+` IS NOT NULL
	    GROUP BY bin ORDER BY bin`,
		emID, from, to, h.LoMs, h.HiMs, stepHistogramBins)
	if err != nil {
		return h, err
	}
	defer rows.Close()
	for rows.Next() {
		var bin, n int
		if err := rows.Scan(&bin, &n); err != nil {
			return h, err
		}
		switch {
		case bin > stepHistogramBins:
			h.Overflow += n
		case bin < 1:
			h.Bins[0] += n
		default:
			h.Bins[bin-1] += n
		}
	}
	return h, rows.Err()
}

func (s *Server) cycleDrift(ctx context.Context, emID int, from, to time.Time,
	bucket time.Duration, metric string) ([]DriftPoint, error) {
	col := cycleMetrics[metric]
	rows, err := s.pool.Query(ctx, `
	    SELECT to_timestamp(floor(extract(epoch FROM start_ts) / $4) * $4) AS bucket_ts,
	           count(*)::int,
	           percentile_cont(0.25) WITHIN GROUP (ORDER BY `+col+`),
	           percentile_cont(0.50) WITHIN GROUP (ORDER BY `+col+`),
	           percentile_cont(0.75) WITHIN GROUP (ORDER BY `+col+`),
	           percentile_cont(0.95) WITHIN GROUP (ORDER BY `+col+`)
	    FROM cycle WHERE em_id=$1 AND start_ts >= $2 AND start_ts < $3
	      AND `+col+` IS NOT NULL
	    GROUP BY 1 ORDER BY 1`, emID, from, to, int(bucket.Seconds()))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []DriftPoint{}
	for rows.Next() {
		var p DriftPoint
		if err := rows.Scan(&p.BucketTs, &p.Count, &p.P25Ms, &p.P50Ms,
			&p.P75Ms, &p.P95Ms); err != nil {
			return nil, err
		}
		p.BucketTs = p.BucketTs.UTC()
		out = append(out, p)
	}
	return out, rows.Err()
}

type CycleStats struct {
	Count       int      `json:"count"`
	Interrupted int      `json:"interrupted"`
	AvgMs       *float64 `json:"avg_ms,omitempty"`
	P10Ms       *float64 `json:"p10_ms,omitempty"`
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
	           percentile_cont(0.1) WITHIN GROUP (ORDER BY total_ms),
	           percentile_cont(0.5) WITHIN GROUP (ORDER BY total_ms),
	           percentile_cont(0.9) WITHIN GROUP (ORDER BY total_ms),
	           avg(work_ms), avg(exchange_ms)
	    FROM cycle
	    WHERE em_id = ANY($1) AND start_ts >= $2 AND start_ts < $3`,
		ids, from, to).Scan(&cs.Count, &cs.Interrupted, &cs.AvgMs,
		&cs.P10Ms, &cs.P50Ms, &cs.P90Ms, &cs.WorkAvgMs, &cs.ExchAvgMs)
	if err != nil {
		return cs, err
	}
	hours := to.Sub(from).Hours()
	if hours > 0 && cs.Count > 0 {
		ph := round1(float64(cs.Count) / hours)
		cs.PerHour = &ph
	}
	for _, p := range []**float64{&cs.AvgMs, &cs.P10Ms, &cs.P50Ms, &cs.P90Ms, &cs.WorkAvgMs, &cs.ExchAvgMs} {
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

// bucketDurations are the toggle options the charts offer. The fine end
// exists for the step-drift line chart, which can carry far more points
// than a bar chart.
var bucketDurations = map[string]time.Duration{
	"10s": 10 * time.Second,
	"30s": 30 * time.Second,
	"1m":  time.Minute,
	"2m":  2 * time.Minute,
	"5m":  5 * time.Minute,
	"10m": 10 * time.Minute,
	"15m": 15 * time.Minute,
	"30m": 30 * time.Minute,
	"1h":  time.Hour,
	"2h":  2 * time.Hour,
	"4h":  4 * time.Hour,
	"12h": 12 * time.Hour,
	"1d":  24 * time.Hour,
}

// niceBuckets is the ladder auto-sizing snaps to, ascending.
var niceBuckets = []struct {
	name string
	d    time.Duration
}{
	{"10s", 10 * time.Second}, {"30s", 30 * time.Second},
	{"1m", time.Minute}, {"2m", 2 * time.Minute}, {"5m", 5 * time.Minute},
	{"10m", 10 * time.Minute}, {"15m", 15 * time.Minute}, {"30m", 30 * time.Minute},
	{"1h", time.Hour}, {"2h", 2 * time.Hour}, {"4h", 4 * time.Hour},
	{"12h", 12 * time.Hour}, {"1d", 24 * time.Hour},
}

// driftTargetBuckets is how many points the drift line aims for. At ~300
// each point is well under 1% of the chart width, so the crosshair reads as
// continuous rather than stepping between hours. A few hundred points is
// nothing for an SVG path or for one GROUP BY.
const driftTargetBuckets = 300

// minSamplesPerBucket keeps percentiles meaningful. The binding limit on
// granularity is NOT query cost — Postgres groups tens of thousands of rows
// without noticing — it is samples: p95 of three executions is just the
// max, so a chart bucketed finer than the data lies with a straight face.
const minSamplesPerBucket = 8

// autoStepDriftBucket sizes the drift bucket from BOTH the window span (so
// the line has ~driftTargetBuckets points regardless of range) and the
// execution count (so each bucket still holds enough samples to have
// percentiles). The coarser of the two wins.
func autoStepDriftBucket(from, to time.Time, n int) (string, time.Duration) {
	span := to.Sub(from)
	if span <= 0 {
		return "1h", time.Hour
	}
	want := span / driftTargetBuckets
	if n > 0 {
		maxBuckets := n / minSamplesPerBucket
		if maxBuckets < 1 {
			maxBuckets = 1
		}
		if bySamples := span / time.Duration(maxBuckets); bySamples > want {
			want = bySamples
		}
	}
	for _, b := range niceBuckets {
		if b.d >= want {
			return b.name, b.d
		}
	}
	last := niceBuckets[len(niceBuckets)-1]
	return last.name, last.d
}

func parseBucket(s string) (string, time.Duration) {
	if d, ok := bucketDurations[s]; ok {
		return s, d
	}
	return "1h", time.Hour // default: per hour
}

// autoFlowBucket picks a chart bucket width from the window span so a
// typical view stays around ~8–48 bars.
func autoFlowBucket(from, to time.Time) (string, time.Duration) {
	span := to.Sub(from)
	switch {
	case span <= 4*time.Hour:
		return "15m", 15 * time.Minute
	case span <= 36*time.Hour:
		return "1h", time.Hour
	case span <= 7*24*time.Hour:
		return "4h", 4 * time.Hour
	default:
		return "1d", 24 * time.Hour
	}
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

// FlowReasonAgg is one starved/blocked reason for a single EM: minutes and
// occurrences of that exact waiting_on / reason text within the window.
type FlowReasonAgg struct {
	Reason  string  `json:"reason"`
	State   string  `json:"state"` // starved | blocked
	Minutes float64 `json:"minutes"`
	Count   int     `json:"count"`
}

func (s *Server) flowReasons(ctx context.Context, emID int, from, to time.Time) ([]FlowReasonAgg, error) {
	rows, err := s.pool.Query(ctx, `
	    SELECT COALESCE(NULLIF(BTRIM(reason), ''), '(no reason reported)'),
	           state,
	           SUM(EXTRACT(EPOCH FROM (LEAST(end_ts,$3) - GREATEST(start_ts,$2)))/60.0),
	           count(*)::int
	    FROM state_interval
	    WHERE em_id=$1 AND state IN ('starved','blocked')
	      AND end_ts > $2 AND start_ts < $3
	    GROUP BY 1, 2`, emID, from, to)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	type key struct{ reason, state string }
	agg := map[key]*FlowReasonAgg{}
	for rows.Next() {
		var reason, state string
		var minutes float64
		var count int
		if err := rows.Scan(&reason, &state, &minutes, &count); err != nil {
			return nil, err
		}
		k := key{reason, state}
		agg[k] = &FlowReasonAgg{Reason: reason, State: state, Minutes: minutes, Count: count}
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}

	// Fold the live open interval so "today" includes the wait happening now.
	for _, le := range s.live() {
		if le.EMID != emID {
			continue
		}
		if le.State != model.StateStarved && le.State != model.StateBlocked {
			continue
		}
		ms := overlapMs(le.Since, time.Now().UTC(), from, to)
		if ms <= 0 {
			continue
		}
		reason := strings.TrimSpace(le.Reason)
		if reason == "" {
			reason = "(no reason reported)"
		}
		k := key{reason, le.State}
		if a := agg[k]; a != nil {
			a.Minutes += float64(ms) / 60000.0
			a.Count++
		} else {
			agg[k] = &FlowReasonAgg{
				Reason: reason, State: le.State,
				Minutes: float64(ms) / 60000.0, Count: 1,
			}
		}
	}

	out := make([]FlowReasonAgg, 0, len(agg))
	for _, a := range agg {
		a.Minutes = round1(a.Minutes)
		out = append(out, *a)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Minutes != out[j].Minutes {
			return out[i].Minutes > out[j].Minutes
		}
		return out[i].Count > out[j].Count
	})
	if len(out) > 15 {
		out = out[:15]
	}
	return out, nil
}

// FlowReasonTimeline is starved/blocked minutes by reason across time
// buckets — used to show how waiting-on reasons shift over a window.
type FlowReasonTimeline struct {
	Bucket  string             `json:"bucket"`  // e.g. "1h"
	Buckets []time.Time        `json:"buckets"` // bucket starts, ascending
	Series  []FlowReasonSeries `json:"series"`  // top reasons (+ Other); values align with Buckets
}

type FlowReasonSeries struct {
	Reason  string    `json:"reason"`
	State   string    `json:"state"` // starved | blocked | other
	Minutes []float64 `json:"minutes"`
}

const flowTimelineTopN = 6

// bucketRange returns the first and last bucket start covering [from,to].
//
// The last bucket is the one CONTAINING to — i.e. the current, partial
// bucket is included. Stopping a bucket short (to - bucket) silently drops
// every wait that ended inside the current bucket: while the wait is open it
// is folded in from the live snapshot below and shows on the chart, then the
// moment it closes and moves to state_interval it has no bucket to join to
// and disappears until the clock rolls into the next bucket. The SUM in the
// query clips to `to`, so a partial bucket reports only its elapsed part.
func bucketRange(from, to time.Time, bucket time.Duration) (start, last time.Time) {
	start = from.UTC().Truncate(bucket)
	last = to.UTC().Truncate(bucket)
	if last.Before(start) {
		last = start
	}
	return start, last
}

func (s *Server) flowReasonsTimeline(ctx context.Context, emID int, from, to time.Time,
	bucketName string, bucket time.Duration) (FlowReasonTimeline, error) {
	empty := FlowReasonTimeline{Bucket: bucketName, Buckets: []time.Time{}, Series: []FlowReasonSeries{}}
	if !to.After(from) || bucket <= 0 {
		return empty, nil
	}

	start, seriesStop := bucketRange(from, to, bucket)

	rows, err := s.pool.Query(ctx, `
	    WITH buckets AS (
	      SELECT generate_series(
	        $2::timestamptz,
	        $3::timestamptz,
	        make_interval(secs => $4)
	      ) AS bucket_ts
	    )
	    SELECT b.bucket_ts,
	           COALESCE(NULLIF(BTRIM(si.reason), ''), '(no reason reported)'),
	           si.state,
	           SUM(EXTRACT(EPOCH FROM (
	             LEAST(si.end_ts, b.bucket_ts + make_interval(secs => $4), $5) -
	             GREATEST(si.start_ts, b.bucket_ts, $6)
	           ))/60.0)
	    FROM buckets b
	    JOIN state_interval si
	      ON si.em_id = $1
	     AND si.state IN ('starved','blocked')
	     AND si.end_ts > GREATEST(b.bucket_ts, $6)
	     AND si.start_ts < LEAST(b.bucket_ts + make_interval(secs => $4), $5)
	    GROUP BY 1, 2, 3
	    HAVING SUM(EXTRACT(EPOCH FROM (
	             LEAST(si.end_ts, b.bucket_ts + make_interval(secs => $4), $5) -
	             GREATEST(si.start_ts, b.bucket_ts, $6)
	           ))/60.0) > 0.001`,
		emID, start, seriesStop, int(bucket.Seconds()), to, from)
	if err != nil {
		return empty, err
	}
	defer rows.Close()

	type key struct{ reason, state string }
	type cell struct {
		bucket time.Time
		key    key
		min    float64
	}
	var cells []cell
	totals := map[key]float64{}
	for rows.Next() {
		var bts time.Time
		var reason, state string
		var minutes float64
		if err := rows.Scan(&bts, &reason, &state, &minutes); err != nil {
			return empty, err
		}
		k := key{reason, state}
		cells = append(cells, cell{bts.UTC(), k, minutes})
		totals[k] += minutes
	}
	if err := rows.Err(); err != nil {
		return empty, err
	}

	// Fold the live open interval into its current bucket.
	for _, le := range s.live() {
		if le.EMID != emID {
			continue
		}
		if le.State != model.StateStarved && le.State != model.StateBlocked {
			continue
		}
		ms := overlapMs(le.Since, time.Now().UTC(), from, to)
		if ms <= 0 {
			continue
		}
		reason := strings.TrimSpace(le.Reason)
		if reason == "" {
			reason = "(no reason reported)"
		}
		k := key{reason, le.State}
		// Attribute the open overlap to the bucket of "now" (clipped into window).
		now := time.Now().UTC()
		if now.After(to) {
			now = to.Add(-time.Nanosecond)
		}
		if now.Before(from) {
			continue
		}
		bts := now.Truncate(bucket)
		if bts.Before(start) {
			bts = start
		}
		cells = append(cells, cell{bts, k, float64(ms) / 60000.0})
		totals[k] += float64(ms) / 60000.0
	}

	if len(totals) == 0 {
		return empty, nil
	}

	// Continuous bucket axis covering the window, inclusive of the current
	// partial bucket (seriesStop is the bucket containing `to`).
	buckets := []time.Time{}
	for t := start; !t.After(seriesStop); t = t.Add(bucket) {
		buckets = append(buckets, t)
	}
	if len(buckets) == 0 {
		return empty, nil
	}
	idx := map[int64]int{}
	for i, b := range buckets {
		idx[b.UnixMilli()] = i
	}

	// Top N reasons by total minutes; remainder collapses to Other.
	type ranked struct {
		k key
		m float64
	}
	rank := make([]ranked, 0, len(totals))
	for k, m := range totals {
		rank = append(rank, ranked{k, m})
	}
	sort.Slice(rank, func(i, j int) bool { return rank[i].m > rank[j].m })
	keep := map[key]bool{}
	seriesKeys := []key{}
	for i, r := range rank {
		if i >= flowTimelineTopN {
			break
		}
		keep[r.k] = true
		seriesKeys = append(seriesKeys, r.k)
	}
	hasOther := len(rank) > len(seriesKeys)

	series := make([]FlowReasonSeries, 0, len(seriesKeys)+1)
	seriesIdx := map[key]int{}
	for _, k := range seriesKeys {
		seriesIdx[k] = len(series)
		series = append(series, FlowReasonSeries{
			Reason: k.reason, State: k.state,
			Minutes: make([]float64, len(buckets)),
		})
	}
	otherIdx := -1
	if hasOther {
		otherIdx = len(series)
		series = append(series, FlowReasonSeries{
			Reason: "Other", State: "other",
			Minutes: make([]float64, len(buckets)),
		})
	}

	for _, c := range cells {
		bi, ok := idx[c.bucket.UnixMilli()]
		if !ok {
			continue
		}
		if si, ok := seriesIdx[c.key]; ok {
			series[si].Minutes[bi] += c.min
		} else if otherIdx >= 0 {
			series[otherIdx].Minutes[bi] += c.min
		}
	}
	for i := range series {
		for j, v := range series[i].Minutes {
			series[i].Minutes[j] = round1(v)
		}
	}

	return FlowReasonTimeline{Bucket: bucketName, Buckets: buckets, Series: series}, nil
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
	return rankReasons(agg, n)
}

// composedDownReasons attributes LINE downtime to sticky episode reasons
// using the composed-down timeline. For each composed-down segment, episode
// intervals that overlap it are UNIONED per reason — so eight EMs sharing
// "air pressure" for 10 minutes contribute 10 minutes, not 80. Episodes that
// fall only while the line is still up (redundancy covering) do not count.
// Minutes are optionally clipped to production ranges (pass nil for none).
// Count is the number of composed-down segments that carried the reason.
func composedDownReasons(downs []compose.DownSeg, eps []EpisodeRow, prod []compose.Span, n int) []ReasonAgg {
	agg := map[string]*ReasonAgg{}
	for _, d := range downs {
		byKey := map[string][]compose.Span{}
		meta := map[string]ReasonAgg{}
		for _, e := range eps {
			lo := max64(e.StartTs.UnixMilli(), d.Start)
			hi := min64(e.EndTs.UnixMilli(), d.End)
			if hi <= lo {
				continue
			}
			key := e.ReasonType + "|" + e.Reason
			byKey[key] = append(byKey[key], compose.Span{Start: lo, End: hi})
			if _, ok := meta[key]; !ok {
				meta[key] = ReasonAgg{Reason: e.Reason, ReasonType: e.ReasonType}
			}
		}
		for key, spans := range byKey {
			var ms int64
			for _, sp := range mergeSpans(spans) {
				ms += compose.ClipMs(sp, prod)
			}
			if ms == 0 {
				continue
			}
			if agg[key] == nil {
				a := meta[key]
				agg[key] = &a
			}
			agg[key].Count++
			agg[key].Minutes = round1(agg[key].Minutes + float64(ms)/60000.0)
		}
	}
	return rankReasons(agg, n)
}

func rankReasons(agg map[string]*ReasonAgg, n int) []ReasonAgg {
	out := make([]ReasonAgg, 0, len(agg))
	for _, a := range agg {
		out = append(out, *a)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Minutes > out[j].Minutes })
	if n > 0 && len(out) > n {
		out = out[:n]
	}
	return out
}

func max64(a, b int64) int64 {
	if a > b {
		return a
	}
	return b
}

func min64(a, b int64) int64 {
	if a < b {
		return a
	}
	return b
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
