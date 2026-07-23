package api

import (
	"context"
	"net/http"
	"strconv"
	"strings"
	"time"

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
	for _, l := range s.lines {
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
