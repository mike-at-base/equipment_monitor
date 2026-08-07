package api

// Side-by-side comparison of an arbitrary set of EMs, in a single request.
//
// Deliberately NOT scoped to a line. "How does the press on CELL1 compare to
// the press on CELL2" is one of the more useful questions a dashboard can
// answer, and a line-scoped endpoint cannot express it. Each EM is named by a
// fully qualified LINE/STATION/label reference.
//
// The comparison math stays here rather than in the browser: the dashboard's
// multi-EM widgets would otherwise fan out N requests and re-derive
// availability client-side, which is exactly what the "no math in the UI" rule
// in api.go exists to prevent. Percentiles are computed per EM with one
// GROUP BY query — cycleStats aggregates its ids into a single row, so it
// cannot be reused for this.

import (
	"context"
	"net/http"
	"strings"
	"time"
)

// EMCompareRow is one EM's numbers over the window. An EM with no data in the
// window still gets a row: the widgets need the label present so a silent EM
// reads as a gap rather than going missing.
type EMCompareRow struct {
	Ref     string `json:"ref"` // "LINE/STATION/label"
	Line    string `json:"line"`
	Station string `json:"station"`
	EMLabel string `json:"em_label"`
	Display string `json:"display_name"`

	AvailabilityPct *float64           `json:"availability_pct,omitempty"`
	StateMin        map[string]float64 `json:"state_min"`

	Cycles CycleStats `json:"cycles"`
	// the same percentile set the single-EM spread chart uses, so a
	// comparison box plot and the per-EM page cannot disagree
	Spread *CycleSpread  `json:"spread,omitempty"`
	Ivals  []CompareIval `json:"intervals,omitempty"`
}

type CompareIval struct {
	StartTs time.Time `json:"start_ts"`
	EndTs   time.Time `json:"end_ts"`
	State   string    `json:"state"`
	Reason  string    `json:"reason,omitempty"`
}

type EMCompareResp struct {
	From time.Time      `json:"from"`
	To   time.Time      `json:"to"`
	EMs  []EMCompareRow `json:"ems"`
	// EMs named in ?ems= that no longer exist — a dashboard can outlive its
	// equipment, and the UI should say so rather than silently drop rows.
	Missing []string `json:"missing,omitempty"`
}

// handleEMCompare serves GET /api/v2/emcompare?ems=LINE/STATION/label,...
func (s *Server) handleEMCompare(w http.ResponseWriter, r *http.Request) {
	from, to, err := s.window(r)
	if err != nil {
		httpErr(w, 400, err)
		return
	}
	refs := splitRefs(r.URL.Query().Get("ems"))
	if len(refs) == 0 {
		httpErr(w, 400, jsonErr("ems= required, comma-separated LINE/STATION/label"))
		return
	}
	if len(refs) > 40 {
		httpErr(w, 400, jsonErr("at most 40 EMs"))
		return
	}
	resp, err := s.emCompare(r.Context(), refs, from, to,
		r.URL.Query().Get("intervals") == "1")
	if err != nil {
		httpErr(w, 500, err)
		return
	}
	writeJSON(w, resp)
}

func (s *Server) emCompare(ctx context.Context, refs []string,
	from, to time.Time, withIvals bool) (*EMCompareResp, error) {

	out := &EMCompareResp{From: from, To: to, EMs: []EMCompareRow{}}

	// resolve refs in the order the user chose, so rows stay stable
	var ids []int
	idSet := map[int]bool{}
	byID := map[int]EMInfo{}
	rowOf := map[int]int{} // em id → index into out.EMs
	byLine := map[string][]int{}
	for _, ref := range refs {
		line, station, label, ok := splitEMRef(ref)
		if !ok {
			out.Missing = append(out.Missing, ref)
			continue
		}
		l, em := s.findEM(line, station, label)
		if em == nil {
			out.Missing = append(out.Missing, ref)
			continue
		}
		if _, dup := rowOf[em.ID]; dup {
			continue // the same EM twice would draw two identical rows
		}
		ids = append(ids, em.ID)
		idSet[em.ID] = true
		byID[em.ID] = *em
		byLine[l.Name] = append(byLine[l.Name], em.ID)
		rowOf[em.ID] = len(out.EMs)
		out.EMs = append(out.EMs, EMCompareRow{
			Ref: ref, Line: l.Name, Station: em.Station, EMLabel: em.Label,
			Display: em.Display, StateMin: map[string]float64{},
		})
	}
	if len(ids) == 0 {
		return out, nil
	}

	// wall-clock time by state (display), including the open interval
	stateMs, err := s.stateMinutes(ctx, ids, from, to)
	if err != nil {
		return nil, err
	}
	s.openContribution(idSet, from, to, stateMs)
	for id, ms := range stateMs {
		i, ok := rowOf[id]
		if !ok {
			continue
		}
		for st, v := range ms {
			out.EMs[i].StateMin[st] = round1(float64(v) / 60000.0)
		}
	}

	// Availability is measured over production time and the schedule is per
	// line, so this part cannot be done in one pass over every EM.
	if err := s.compareAvailability(ctx, byLine, byID, rowOf, out, from, to); err != nil {
		return nil, err
	}
	if err := s.compareCycles(ctx, ids, rowOf, out, from, to); err != nil {
		return nil, err
	}
	if withIvals {
		if err := s.compareIntervals(ctx, ids, rowOf, out, from, to); err != nil {
			return nil, err
		}
	}
	return out, nil
}

// compareAvailability computes each EM's episode availability the same way the
// line summary does, one line at a time because production ranges differ.
func (s *Server) compareAvailability(ctx context.Context, byLine map[string][]int,
	byID map[int]EMInfo, rowOf map[int]int, out *EMCompareResp,
	from, to time.Time) error {

	for line, ids := range byLine {
		ranges, err := s.lineProductionRanges(ctx, line, from, to)
		if err != nil {
			return err
		}
		idSet := map[int]bool{}
		lineByID := map[int]EMInfo{}
		for _, id := range ids {
			idSet[id] = true
			lineByID[id] = byID[id]
		}
		agg, err := s.prodStateAgg(ctx, ids, from, to, idSet, ranges)
		if err != nil {
			return err
		}
		episodes, err := s.episodeRows(ctx, ids, lineByID, from, to, 5000)
		if err != nil {
			return err
		}
		epMs := map[int]int64{}
		for _, e := range episodes {
			for id, info := range lineByID {
				if info.Station == e.Station && info.Label == e.EMLabel {
					epMs[id] += intersectMs(e.StartTs, e.EndTs, ranges)
				}
			}
		}
		for _, id := range ids {
			a := agg[id]
			if a == nil {
				a = &availAgg{}
			}
			ep := epMs[id]
			if ep < a.down {
				ep = a.down // every down second belongs to some episode
			}
			if pct, ok := episodeAvailability(a.avail, a.down, ep); ok {
				p := round1(pct)
				out.EMs[rowOf[id]].AvailabilityPct = &p
			}
		}
	}
	return nil
}

func (s *Server) compareCycles(ctx context.Context, ids []int, rowOf map[int]int,
	out *EMCompareResp, from, to time.Time) error {

	rows, err := s.pool.Query(ctx, `
	    SELECT em_id, count(*), count(*) FILTER (WHERE interrupted),
	           avg(total_ms),
	           percentile_cont(0.10) WITHIN GROUP (ORDER BY total_ms),
	           percentile_cont(0.50) WITHIN GROUP (ORDER BY total_ms),
	           percentile_cont(0.90) WITHIN GROUP (ORDER BY total_ms),
	           avg(work_ms), avg(exchange_ms),
	           min(total_ms), max(total_ms),
	           percentile_cont(0.05) WITHIN GROUP (ORDER BY total_ms),
	           percentile_cont(0.25) WITHIN GROUP (ORDER BY total_ms),
	           percentile_cont(0.75) WITHIN GROUP (ORDER BY total_ms),
	           percentile_cont(0.95) WITHIN GROUP (ORDER BY total_ms)
	    FROM cycle
	    WHERE em_id = ANY($1) AND start_ts >= $2 AND start_ts < $3
	    GROUP BY em_id`, ids, from, to)
	if err != nil {
		return err
	}
	defer rows.Close()
	hours := to.Sub(from).Hours()
	for rows.Next() {
		var id int
		var cs CycleStats
		var sp CycleSpread
		var minMs, maxMs *float64
		if err := rows.Scan(&id, &cs.Count, &cs.Interrupted, &cs.AvgMs,
			&cs.P10Ms, &cs.P50Ms, &cs.P90Ms, &cs.WorkAvgMs, &cs.ExchAvgMs,
			&minMs, &maxMs, &sp.P05Ms, &sp.P25Ms, &sp.P75Ms, &sp.P95Ms); err != nil {
			return err
		}
		i, ok := rowOf[id]
		if !ok {
			continue
		}
		if hours > 0 && cs.Count > 0 {
			ph := round1(float64(cs.Count) / hours)
			cs.PerHour = &ph
		}
		for _, p := range []**float64{&cs.AvgMs, &cs.P10Ms, &cs.P50Ms, &cs.P90Ms,
			&cs.WorkAvgMs, &cs.ExchAvgMs} {
			if *p != nil {
				v := round1(**p)
				*p = &v
			}
		}
		out.EMs[i].Cycles = cs
		if cs.Count > 0 && minMs != nil && maxMs != nil {
			sp.Name = out.EMs[i].Ref
			sp.Count = cs.Count
			// the median is already selected for CycleStats; the box plot
			// uses that same value rather than asking Postgres twice
			if cs.P50Ms != nil {
				sp.P50Ms = *cs.P50Ms
			}
			sp.MinMs, sp.MaxMs = *minMs, *maxMs
			out.EMs[i].Spread = &sp
		}
	}
	return rows.Err()
}

// compareIntervals feeds the state-timeline widget. Capped overall: a Gantt
// squashes rather than scrolls, and thousands of slivers render as mud.
func (s *Server) compareIntervals(ctx context.Context, ids []int, rowOf map[int]int,
	out *EMCompareResp, from, to time.Time) error {

	rows, err := s.pool.Query(ctx, `
	    SELECT em_id, GREATEST(start_ts,$2), LEAST(end_ts,$3), state, COALESCE(reason,'')
	    FROM state_interval
	    WHERE em_id = ANY($1) AND end_ts > $2 AND start_ts < $3
	    ORDER BY em_id, start_ts
	    LIMIT 20000`, ids, from, to)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var id int
		var iv CompareIval
		if err := rows.Scan(&id, &iv.StartTs, &iv.EndTs, &iv.State, &iv.Reason); err != nil {
			return err
		}
		i, ok := rowOf[id]
		if !ok {
			continue
		}
		out.EMs[i].Ivals = append(out.EMs[i].Ivals, iv)
	}
	if err := rows.Err(); err != nil {
		return err
	}
	// the open interval is in memory, not yet in state_interval
	now := time.Now().UTC()
	for _, le := range s.live() {
		i, ok := rowOf[le.EMID]
		if !ok || le.State == "" || le.Since.IsZero() {
			continue
		}
		start := le.Since
		if start.Before(from) {
			start = from
		}
		end := now
		if end.After(to) {
			end = to
		}
		if !end.After(start) {
			continue
		}
		out.EMs[i].Ivals = append(out.EMs[i].Ivals, CompareIval{
			StartTs: start, EndTs: end, State: le.State, Reason: le.Reason,
		})
	}
	return nil
}

func splitRefs(s string) []string {
	var out []string
	for _, p := range strings.Split(s, ",") {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}
