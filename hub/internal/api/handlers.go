package api

import (
	"context"
	"encoding/json"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/mike-at-base/equipment_monitor/hub/internal/ingest"
	"github.com/mike-at-base/equipment_monitor/hub/internal/model"
)

// ── line summary (shared by /summary and /compare) ───────────────────────

type EMSummary struct {
	Station         string             `json:"station"`
	EMLabel         string             `json:"em_label"`
	Display         string             `json:"display_name"`
	AvailabilityPct *float64           `json:"availability_pct,omitempty"`
	StateMin        map[string]float64 `json:"state_min"`
}

type MTTR struct {
	Downs         int      `json:"downs"`
	AvgMin        *float64 `json:"avg_min,omitempty"`
	Acked         int      `json:"acked"`
	ResponseAvgMn *float64 `json:"response_avg_min,omitempty"`
	RepairAvgMin  *float64 `json:"repair_avg_min,omitempty"`
}

type EpisodeStats struct {
	Count   int     `json:"count"`
	Minutes float64 `json:"minutes"`
	Retries int     `json:"retries"`
	Ongoing int     `json:"ongoing"`
}

type LineSummary struct {
	Line            string             `json:"line"`
	From            time.Time          `json:"from"`
	To              time.Time          `json:"to"`
	AvailabilityPct *float64           `json:"availability_pct,omitempty"`
	StateMin        map[string]float64 `json:"state_min"`
	Episodes        EpisodeStats       `json:"episodes"`
	Cycles          CycleStats         `json:"cycles"`
	TopDownReasons  []ReasonAgg        `json:"top_down_reasons"`
	FlowLosses      []FlowAgg          `json:"flow_losses"`
	ModeMin         map[string]float64 `json:"mode_min"`
	MTTR            MTTR               `json:"mttr"`
	EMs             []EMSummary        `json:"ems"`
}

func (s *Server) lineSummary(ctx context.Context, l *LineInfo, from, to time.Time) (*LineSummary, error) {
	ids := make([]int, 0, len(l.EMs))
	idSet := map[int]bool{}
	byID := map[int]EMInfo{}
	for _, e := range l.EMs {
		ids = append(ids, e.ID)
		idSet[e.ID] = true
		byID[e.ID] = e
	}

	stateMs, err := s.stateMinutes(ctx, ids, from, to)
	if err != nil {
		return nil, err
	}
	s.openContribution(idSet, from, to, stateMs)

	episodes, err := s.episodeRows(ctx, ids, byID, from, to, 5000)
	if err != nil {
		return nil, err
	}
	cycles, err := s.cycleStats(ctx, ids, from, to)
	if err != nil {
		return nil, err
	}
	flow, err := s.flowLosses(ctx, ids, byID, from, to)
	if err != nil {
		return nil, err
	}
	modes, err := s.modeMinutes(ctx, ids, from, to)
	if err != nil {
		return nil, err
	}

	sum := &LineSummary{
		Line: l.Name, From: from, To: to,
		EMs:            []EMSummary{},
		StateMin:       map[string]float64{},
		Cycles:         cycles,
		TopDownReasons: topEpisodeReasons(episodes, 5),
		FlowLosses:     flow,
		ModeMin:        modes,
	}

	// per-EM episode minutes for episode-based availability
	epMsByEM := map[int]int64{}
	for _, e := range episodes {
		for id, info := range byID {
			if info.Station == e.Station && info.Label == e.EMLabel {
				epMsByEM[id] += int64(e.Minutes * 60000)
			}
		}
		sum.Episodes.Count++
		sum.Episodes.Minutes = round1(sum.Episodes.Minutes + e.Minutes)
		sum.Episodes.Retries += e.Retries
		if e.Ongoing {
			sum.Episodes.Ongoing++
		}
	}

	lineMs := map[string]int64{}
	var lineAvailRaw, lineDown, lineEp int64
	for _, e := range l.EMs {
		ms := stateMs[e.ID]
		em := EMSummary{Station: e.Station, EMLabel: e.Label, Display: e.Display,
			StateMin: map[string]float64{}}
		var availRaw int64
		for st, v := range ms {
			em.StateMin[st] = round1(float64(v) / 60000.0)
			lineMs[st] += v
			if strings.Contains(availStates, st) {
				availRaw += v
			}
		}
		down := ms[model.StateDown]
		ep := epMsByEM[e.ID]
		if ep < down {
			ep = down // every raw down second belongs to some episode
		}
		lineAvailRaw += availRaw
		lineDown += down
		lineEp += ep
		if pct, ok := episodeAvailability(availRaw, down, ep); ok {
			p := round1(pct)
			em.AvailabilityPct = &p
		}
		sum.EMs = append(sum.EMs, em)
	}
	for st, v := range lineMs {
		sum.StateMin[st] = round1(float64(v) / 60000.0)
	}
	if pct, ok := episodeAvailability(lineAvailRaw, lineDown, lineEp); ok {
		p := round1(pct)
		sum.AvailabilityPct = &p
	}

	// MTTR decomposition — per EPISODE (sticky root cause), not raw interval
	sum.MTTR.Downs = len(episodes)
	var tot, resp, rep float64
	for _, d := range episodes {
		tot += d.Minutes
		if d.RespMin != nil {
			sum.MTTR.Acked++
			resp += *d.RespMin
			rep += *d.RepairMin
		}
	}
	if len(episodes) > 0 {
		a := round1(tot / float64(len(episodes)))
		sum.MTTR.AvgMin = &a
	}
	if sum.MTTR.Acked > 0 {
		r := round1(resp / float64(sum.MTTR.Acked))
		p := round1(rep / float64(sum.MTTR.Acked))
		sum.MTTR.ResponseAvgMn = &r
		sum.MTTR.RepairAvgMin = &p
	}
	return sum, nil
}

// ── handlers ─────────────────────────────────────────────────────────────

func (s *Server) handleLines(w http.ResponseWriter, r *http.Request) {
	type liveCount map[string]int
	counts := map[string]liveCount{}
	for _, le := range s.live() {
		if counts[le.Line] == nil {
			counts[le.Line] = liveCount{}
		}
		st := le.State
		if st == "" {
			st = "no_data"
		}
		counts[le.Line][st]++
	}
	type lineOut struct {
		Name    string         `json:"name"`
		EMCount int            `json:"em_count"`
		Live    map[string]int `json:"live_states"`
	}
	out := []lineOut{}
	for _, l := range s.snapshotLines() {
		out = append(out, lineOut{Name: l.Name, EMCount: len(l.EMs), Live: counts[l.Name]})
	}
	writeJSON(w, out)
}

func (s *Server) handleLineSummary(w http.ResponseWriter, r *http.Request) {
	l := s.findLine(r.PathValue("line"))
	if l == nil {
		httpErr(w, 404, jsonErr("unknown line"))
		return
	}
	from, to, err := s.window(r)
	if err != nil {
		httpErr(w, 400, err)
		return
	}
	sum, err := s.lineSummary(r.Context(), l, from, to)
	if err != nil {
		httpErr(w, 500, err)
		return
	}
	writeJSON(w, sum)
}

func (s *Server) handleCompare(w http.ResponseWriter, r *http.Request) {
	la := s.findLine(r.URL.Query().Get("a"))
	lb := s.findLine(r.URL.Query().Get("b"))
	if la == nil || lb == nil {
		httpErr(w, 400, jsonErr("compare requires ?a= and ?b= (known line names)"))
		return
	}
	from, to, err := s.window(r)
	if err != nil {
		httpErr(w, 400, err)
		return
	}
	sa, err := s.lineSummary(r.Context(), la, from, to)
	if err != nil {
		httpErr(w, 500, err)
		return
	}
	sb, err := s.lineSummary(r.Context(), lb, from, to)
	if err != nil {
		httpErr(w, 500, err)
		return
	}

	delta := map[string]any{}
	if sa.AvailabilityPct != nil && sb.AvailabilityPct != nil {
		delta["availability_pct"] = round1(*sa.AvailabilityPct - *sb.AvailabilityPct)
	}
	delta["cycles"] = sa.Cycles.Count - sb.Cycles.Count
	if sa.Cycles.P50Ms != nil && sb.Cycles.P50Ms != nil {
		delta["cycle_p50_ms"] = round1(*sa.Cycles.P50Ms - *sb.Cycles.P50Ms)
	}
	delta["down_min"] = round1(sa.StateMin["down"] - sb.StateMin["down"])
	delta["starved_min"] = round1(sa.StateMin["starved"] - sb.StateMin["starved"])
	delta["blocked_min"] = round1(sa.StateMin["blocked"] - sb.StateMin["blocked"])

	writeJSON(w, map[string]any{
		"from": from, "to": to,
		"a": sa, "b": sb,
		"delta_a_minus_b": delta,
	})
}

func (s *Server) emIDOr404(w http.ResponseWriter, r *http.Request) (*EMInfo, bool) {
	_, em := s.findEM(r.PathValue("line"), r.PathValue("station"), r.PathValue("label"))
	if em == nil {
		httpErr(w, 404, jsonErr("unknown em"))
		return nil, false
	}
	return em, true
}

func (s *Server) handleIntervals(w http.ResponseWriter, r *http.Request) {
	em, ok := s.emIDOr404(w, r)
	if !ok {
		return
	}
	from, to, err := s.window(r)
	if err != nil {
		httpErr(w, 400, err)
		return
	}
	q := `SELECT start_ts, end_ts, state, reason_type, reason,
	             COALESCE(step_name,''), ack_ts
	      FROM state_interval
	      WHERE em_id = $1 AND end_ts > $2 AND start_ts < $3`
	args := []any{em.ID, from, to}
	if st := r.URL.Query().Get("state"); st != "" {
		q += ` AND state = $4`
		args = append(args, st)
	}
	q += ` ORDER BY start_ts DESC LIMIT 2000`
	rows, err := s.pool.Query(r.Context(), q, args...)
	if err != nil {
		httpErr(w, 500, err)
		return
	}
	defer rows.Close()
	type iv struct {
		StartTs    time.Time  `json:"start_ts"`
		EndTs      time.Time  `json:"end_ts"`
		State      string     `json:"state"`
		ReasonType string     `json:"reason_type,omitempty"`
		Reason     string     `json:"reason,omitempty"`
		StepName   string     `json:"step_name,omitempty"`
		AckTs      *time.Time `json:"ack_ts,omitempty"`
	}
	out := []iv{}
	for rows.Next() {
		var v iv
		if err := rows.Scan(&v.StartTs, &v.EndTs, &v.State, &v.ReasonType,
			&v.Reason, &v.StepName, &v.AckTs); err != nil {
			httpErr(w, 500, err)
			return
		}
		out = append(out, v)
	}
	writeJSON(w, out)
}

func (s *Server) handleSteps(w http.ResponseWriter, r *http.Request) {
	em, ok := s.emIDOr404(w, r)
	if !ok {
		return
	}
	from, to, err := s.window(r)
	if err != nil {
		httpErr(w, 400, err)
		return
	}
	limit := 1000
	if l := r.URL.Query().Get("limit"); l != "" {
		if n, err := strconv.Atoi(l); err == nil && n > 0 && n <= 20000 {
			limit = n
		}
	}
	rows, err := s.pool.Query(r.Context(), `
	    SELECT start_ts, end_ts, seq_index, step_name, step_desc, duration_ms, was_faulted
	    FROM step_event WHERE em_id=$1 AND start_ts >= $2 AND start_ts < $3
	    ORDER BY start_ts DESC LIMIT $4`, em.ID, from, to, limit)
	if err != nil {
		httpErr(w, 500, err)
		return
	}
	defer rows.Close()
	type step struct {
		StartTs    time.Time `json:"start_ts"`
		EndTs      time.Time `json:"end_ts"`
		SeqIndex   int16     `json:"seq_index"`
		StepName   string    `json:"step"`
		StepDesc   string    `json:"description"`
		DurationMs int64     `json:"duration_ms"`
		WasFaulted bool      `json:"was_faulted"`
	}
	out := []step{}
	for rows.Next() {
		var v step
		if err := rows.Scan(&v.StartTs, &v.EndTs, &v.SeqIndex, &v.StepName,
			&v.StepDesc, &v.DurationMs, &v.WasFaulted); err != nil {
			httpErr(w, 500, err)
			return
		}
		out = append(out, v)
	}
	writeJSON(w, out)
}

func (s *Server) handleCycles(w http.ResponseWriter, r *http.Request) {
	em, ok := s.emIDOr404(w, r)
	if !ok {
		return
	}
	from, to, err := s.window(r)
	if err != nil {
		httpErr(w, 400, err)
		return
	}
	stats, err := s.cycleStats(r.Context(), []int{em.ID}, from, to)
	if err != nil {
		httpErr(w, 500, err)
		return
	}
	rows, err := s.pool.Query(r.Context(), `
	    SELECT start_ts, end_ts, seq_index, work_ms, exchange_ms, total_ms, interrupted
	    FROM cycle WHERE em_id=$1 AND start_ts >= $2 AND start_ts < $3
	    ORDER BY start_ts DESC LIMIT 2000`, em.ID, from, to)
	if err != nil {
		httpErr(w, 500, err)
		return
	}
	defer rows.Close()
	type cyc struct {
		StartTs     time.Time `json:"start_ts"`
		EndTs       time.Time `json:"end_ts"`
		SeqIndex    int16     `json:"seq_index"`
		WorkMs      *int64    `json:"work_ms,omitempty"`
		ExchangeMs  *int64    `json:"exchange_ms,omitempty"`
		TotalMs     int64     `json:"total_ms"`
		Interrupted bool      `json:"interrupted"`
	}
	out := []cyc{}
	for rows.Next() {
		var v cyc
		if err := rows.Scan(&v.StartTs, &v.EndTs, &v.SeqIndex, &v.WorkMs,
			&v.ExchangeMs, &v.TotalMs, &v.Interrupted); err != nil {
			httpErr(w, 500, err)
			return
		}
		out = append(out, v)
	}
	writeJSON(w, map[string]any{"stats": stats, "cycles": out})
}

// handleThroughput returns cycle counts bucketed by ?bucket=15m|30m|1h
// (default 1h) over the window — actual completed-cycle counts, not a rate.
func (s *Server) handleThroughput(w http.ResponseWriter, r *http.Request) {
	em, ok := s.emIDOr404(w, r)
	if !ok {
		return
	}
	from, to, err := s.window(r)
	if err != nil {
		httpErr(w, 400, err)
		return
	}
	name, dur := parseBucket(r.URL.Query().Get("bucket"))
	buckets, err := s.cycleThroughput(r.Context(), []int{em.ID}, from, to, dur)
	if err != nil {
		httpErr(w, 500, err)
		return
	}
	writeJSON(w, map[string]any{"bucket": name, "buckets": buckets})
}

func (s *Server) handleDowns(w http.ResponseWriter, r *http.Request) {
	em, ok := s.emIDOr404(w, r)
	if !ok {
		return
	}
	from, to, err := s.window(r)
	if err != nil {
		httpErr(w, 400, err)
		return
	}
	byID := map[int]EMInfo{em.ID: *em}
	episodes, err := s.episodeRows(r.Context(), []int{em.ID}, byID, from, to, 2000)
	if err != nil {
		httpErr(w, 500, err)
		return
	}
	raw, err := s.downRows(r.Context(), []int{em.ID}, byID, from, to, 2000)
	if err != nil {
		httpErr(w, 500, err)
		return
	}
	// episode-based availability for this EM
	stateMs, err := s.stateMinutes(r.Context(), []int{em.ID}, from, to)
	if err != nil {
		httpErr(w, 500, err)
		return
	}
	s.openContribution(map[int]bool{em.ID: true}, from, to, stateMs)
	var availRaw, epMs int64
	ms := stateMs[em.ID]
	for st, v := range ms {
		if strings.Contains(availStates, st) {
			availRaw += v
		}
	}
	for _, e := range episodes {
		epMs += int64(e.Minutes * 60000)
	}
	if down := ms[model.StateDown]; epMs < down {
		epMs = down
	}
	out := map[string]any{
		"episodes":    episodes,
		"raw_downs":   raw,
		"top_reasons": topEpisodeReasons(episodes, 10),
	}
	if pct, ok := episodeAvailability(availRaw, ms[model.StateDown], epMs); ok {
		out["availability_pct"] = round1(pct)
	}
	writeJSON(w, out)
}

// handleDebug is the engineering raw-data view: the last decoded datagram
// (live), plus recent operator resets, mode windows, and raw state intervals
// within the window. This is the unfiltered picture — every state the
// machine reported, not the episode-collapsed reporting view.
func (s *Server) handleDebug(w http.ResponseWriter, r *http.Request) {
	em, ok := s.emIDOr404(w, r)
	if !ok {
		return
	}
	from, to, err := s.window(r)
	if err != nil {
		httpErr(w, 400, err)
		return
	}

	// live raw datagram from the in-memory tracker
	var live *ingest.RawEM
	if s.raw != nil {
		for _, re := range s.raw() {
			if re.EMID == em.ID {
				rr := re
				live = &rr
				break
			}
		}
	}

	// operator resets
	resets := []map[string]any{}
	rows, err := s.pool.Query(r.Context(), `
	    SELECT ts, event FROM operator_event
	    WHERE em_id=$1 AND ts >= $2 AND ts < $3
	    ORDER BY ts DESC LIMIT 500`, em.ID, from, to)
	if err != nil {
		httpErr(w, 500, err)
		return
	}
	for rows.Next() {
		var ts time.Time
		var ev string
		if err := rows.Scan(&ts, &ev); err != nil {
			rows.Close()
			httpErr(w, 500, err)
			return
		}
		resets = append(resets, map[string]any{"ts": ts, "event": ev})
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		httpErr(w, 500, err)
		return
	}

	// mode windows (raw, not aggregated)
	modes := []map[string]any{}
	rows, err = s.pool.Query(r.Context(), `
	    SELECT flag, start_ts, end_ts FROM mode_interval
	    WHERE em_id=$1 AND end_ts > $2 AND start_ts < $3
	    ORDER BY start_ts DESC LIMIT 500`, em.ID, from, to)
	if err != nil {
		httpErr(w, 500, err)
		return
	}
	for rows.Next() {
		var flag string
		var st, en time.Time
		if err := rows.Scan(&flag, &st, &en); err != nil {
			rows.Close()
			httpErr(w, 500, err)
			return
		}
		modes = append(modes, map[string]any{
			"flag": flag, "start_ts": st, "end_ts": en,
			"minutes": round1(en.Sub(st).Minutes()),
		})
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		httpErr(w, 500, err)
		return
	}

	// raw state intervals (every reported phase)
	states := []map[string]any{}
	rows, err = s.pool.Query(r.Context(), `
	    SELECT start_ts, end_ts, state, reason_type, reason, COALESCE(step_name,'')
	    FROM state_interval
	    WHERE em_id=$1 AND end_ts > $2 AND start_ts < $3
	    ORDER BY start_ts DESC LIMIT 1000`, em.ID, from, to)
	if err != nil {
		httpErr(w, 500, err)
		return
	}
	for rows.Next() {
		var st, en time.Time
		var state, rtype, reason, step string
		if err := rows.Scan(&st, &en, &state, &rtype, &reason, &step); err != nil {
			rows.Close()
			httpErr(w, 500, err)
			return
		}
		states = append(states, map[string]any{
			"start_ts": st, "end_ts": en, "state": state,
			"reason_type": rtype, "reason": reason, "step_name": step,
			"seconds": round1(en.Sub(st).Seconds()),
		})
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		httpErr(w, 500, err)
		return
	}

	writeJSON(w, map[string]any{
		"live": live, "resets": resets, "modes": modes, "states": states,
	})
}

// ── EM config: review & confirm ──────────────────────────────────────────

type seqConfigDTO struct {
	Index         int16  `json:"index"`
	Name          string `json:"name"`
	IsProduction  bool   `json:"is_production"`
	CycleStart    string `json:"cycle_start_step"`
	CycleComplete string `json:"cycle_complete_step"`
}

// handleGetConfig returns an EM's current settings plus the step names
// actually observed per sequence — everything the review & confirm screen
// needs to fill in cycle start/complete without guessing.
func (s *Server) handleGetConfig(w http.ResponseWriter, r *http.Request) {
	em, ok := s.emIDOr404(w, r)
	if !ok {
		return
	}
	ctx := r.Context()
	var displayName, lineName string
	var confirmed bool
	var wireVer int
	err := s.pool.QueryRow(ctx, `
	    SELECT e.display_name, e.confirmed, e.wire_version, l.name
	    FROM em e JOIN line l ON l.id = e.line_id WHERE e.id = $1`,
		em.ID).Scan(&displayName, &confirmed, &wireVer, &lineName)
	if err != nil {
		httpErr(w, 500, err)
		return
	}
	seqs := []seqConfigDTO{}
	rows, err := s.pool.Query(ctx, `
	    SELECT seq_index, name, is_production, cycle_start_step, cycle_complete_step
	    FROM sequence WHERE em_id = $1 ORDER BY seq_index`, em.ID)
	if err != nil {
		httpErr(w, 500, err)
		return
	}
	for rows.Next() {
		var so seqConfigDTO
		if err := rows.Scan(&so.Index, &so.Name, &so.IsProduction, &so.CycleStart, &so.CycleComplete); err != nil {
			rows.Close()
			httpErr(w, 500, err)
			return
		}
		seqs = append(seqs, so)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		httpErr(w, 500, err)
		return
	}
	// observed steps per sequence (from recorded history) for dropdowns
	type observed struct {
		SeqIndex int16    `json:"seq_index"`
		Steps    []string `json:"steps"`
	}
	obs := []observed{}
	orows, err := s.pool.Query(ctx, `
	    SELECT seq_index, array_agg(DISTINCT step_name ORDER BY step_name)
	    FROM step_event WHERE em_id = $1 GROUP BY seq_index ORDER BY seq_index`, em.ID)
	if err != nil {
		httpErr(w, 500, err)
		return
	}
	for orows.Next() {
		var o observed
		if err := orows.Scan(&o.SeqIndex, &o.Steps); err != nil {
			orows.Close()
			httpErr(w, 500, err)
			return
		}
		obs = append(obs, o)
	}
	orows.Close()
	if err := orows.Err(); err != nil {
		httpErr(w, 500, err)
		return
	}
	writeJSON(w, map[string]any{
		"station": em.Station, "em_label": em.Label, "line": lineName,
		"display_name": displayName, "confirmed": confirmed, "wire_version": wireVer,
		"sequences": seqs, "observed": obs,
	})
}

// handleSaveConfig persists the review & confirm screen: display name,
// confirmed flag, and sequence metadata — then reloads the live tracker.
func (s *Server) handleSaveConfig(w http.ResponseWriter, r *http.Request) {
	em, ok := s.emIDOr404(w, r)
	if !ok {
		return
	}
	var body struct {
		DisplayName string         `json:"display_name"`
		Confirmed   bool           `json:"confirmed"`
		Sequences   []seqConfigDTO `json:"sequences"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		httpErr(w, 400, jsonErr("invalid body: "+err.Error()))
		return
	}
	ctx := r.Context()
	if _, err := s.pool.Exec(ctx,
		`UPDATE em SET display_name=$1, confirmed=$2 WHERE id=$3`,
		body.DisplayName, body.Confirmed, em.ID); err != nil {
		httpErr(w, 500, err)
		return
	}
	for _, sq := range body.Sequences {
		if _, err := s.pool.Exec(ctx, `
		    INSERT INTO sequence (em_id, seq_index, name, is_production,
		                          cycle_start_step, cycle_complete_step)
		    VALUES ($1,$2,$3,$4,$5,$6)
		    ON CONFLICT (em_id, seq_index) DO UPDATE SET
		      name=EXCLUDED.name, is_production=EXCLUDED.is_production,
		      cycle_start_step=EXCLUDED.cycle_start_step,
		      cycle_complete_step=EXCLUDED.cycle_complete_step`,
			em.ID, sq.Index, sq.Name, sq.IsProduction, sq.CycleStart, sq.CycleComplete); err != nil {
			httpErr(w, 500, err)
			return
		}
	}
	if s.onConfig != nil {
		s.onConfig(em.ID) // reload tracker seq config + refresh hierarchy
	}
	writeJSON(w, map[string]any{"ok": true})
}

// handleUnconfirmed lists auto-discovered EMs awaiting an engineer's review.
func (s *Server) handleUnconfirmed(w http.ResponseWriter, r *http.Request) {
	rows, err := s.pool.Query(r.Context(), `
	    SELECT l.name, e.station, e.em_label, e.display_name, e.wire_version
	    FROM em e JOIN line l ON l.id = e.line_id
	    WHERE NOT e.confirmed AND e.enabled
	    ORDER BY l.name, e.station, e.em_label`)
	if err != nil {
		httpErr(w, 500, err)
		return
	}
	defer rows.Close()
	type row struct {
		Line        string `json:"line"`
		Station     string `json:"station"`
		EMLabel     string `json:"em_label"`
		Display     string `json:"display_name"`
		WireVersion int    `json:"wire_version"`
	}
	out := []row{}
	for rows.Next() {
		var x row
		if err := rows.Scan(&x.Line, &x.Station, &x.EMLabel, &x.Display, &x.WireVersion); err != nil {
			httpErr(w, 500, err)
			return
		}
		out = append(out, x)
	}
	writeJSON(w, out)
}
