package api

// Side-by-side comparison of composable entities — whole lines, or stations
// within them.
//
// The distinction from emcompare is not cosmetic. An EM's availability is its
// own episode availability; a station's is the k-of-n COMPOSED number, which
// asks "could this station run?" rather than "how did its modules average
// out?". A station with four magazines and one dead one is fine; averaging the
// four says it is 75% healthy, which is wrong in a way that matters.
//
// Flow loss is reported two ways for the same reason. Summing per-EM starved
// minutes counts a line-wide starve once per module — on a seven-module cell
// that is a sevenfold overstatement of elapsed time. So each state carries
// both: `em_min`, the loss accounting, and `wall_min`, the clock time during
// which at least one module was in that state. Neither alone is honest.

import (
	"context"
	"net/http"
	"sort"
	"time"

	"github.com/mike-at-base/equipment_monitor/hub/internal/compose"
)

type FlowState struct {
	State string `json:"state"` // starved | blocked | nva
	// summed across the node's EMs — the loss-accounting number
	EMMin float64 `json:"em_min"`
	// clock time with at least one EM in this state — never double counted
	WallMin float64 `json:"wall_min"`
	Count   int     `json:"count"`
}

type FlowReason struct {
	Reason  string  `json:"reason"`
	State   string  `json:"state"`
	Minutes float64 `json:"minutes"` // EM-minutes
	Count   int     `json:"count"`
}

type NodeCompareRow struct {
	Ref     string `json:"ref"`  // "LINE" or "LINE/STATION"
	Kind    string `json:"kind"` // line | station
	Line    string `json:"line"`
	Station string `json:"station,omitempty"`
	Display string `json:"display_name"`

	// composed k-of-n availability over production time
	AvailabilityPct *float64   `json:"availability_pct,omitempty"`
	DefaultModel    bool       `json:"default_model"`
	ProductionMin   float64    `json:"production_min"`
	DownMin         float64    `json:"down_min"`
	Causes          []causeRow `json:"causes"`

	Flow        []FlowState  `json:"flow"`
	FlowReasons []FlowReason `json:"flow_reasons"`
	EMCount     int          `json:"em_count"`
}

type NodeCompareResp struct {
	From    time.Time        `json:"from"`
	To      time.Time        `json:"to"`
	Nodes   []NodeCompareRow `json:"nodes"`
	Missing []string         `json:"missing,omitempty"`
}

// flowStates are the "running but not producing" states this endpoint splits
// out. Down is deliberately absent: that is the availability number.
var flowStates = []string{"starved", "blocked", "nva"}

// handleNodeCompare serves GET /api/v2/nodecompare?nodes=LINE,LINE/STATION,...
func (s *Server) handleNodeCompare(w http.ResponseWriter, r *http.Request) {
	from, to, err := s.window(r)
	if err != nil {
		httpErr(w, 400, err)
		return
	}
	refs := splitRefs(r.URL.Query().Get("nodes"))
	if len(refs) == 0 {
		httpErr(w, 400, jsonErr("nodes= required, comma-separated LINE or LINE/STATION"))
		return
	}
	if len(refs) > 40 {
		httpErr(w, 400, jsonErr("at most 40 nodes"))
		return
	}
	out := &NodeCompareResp{From: from, To: to, Nodes: []NodeCompareRow{}}
	// production ranges are per line and every node on a line shares them,
	// so resolve each line's schedule once however many stations are listed
	prodCache := map[string][]compose.Span{}
	rangeCache := map[string][][2]time.Time{}

	for _, ref := range refs {
		line, station, ok := splitNodeRef(ref)
		if !ok {
			out.Missing = append(out.Missing, ref)
			continue
		}
		l := s.findLine(line)
		if l == nil {
			out.Missing = append(out.Missing, ref)
			continue
		}
		if _, seen := rangeCache[l.Name]; !seen {
			ranges, err := s.lineProductionRanges(r.Context(), l.Name, from, to)
			if err != nil {
				httpErr(w, 500, err)
				return
			}
			rangeCache[l.Name] = ranges
			prodCache[l.Name] = spansFromRanges(ranges)
		}
		row, err := s.nodeRow(r.Context(), ref, l, station, from, to,
			rangeCache[l.Name], prodCache[l.Name])
		if err != nil {
			httpErr(w, 500, err)
			return
		}
		if row == nil {
			out.Missing = append(out.Missing, ref)
			continue
		}
		out.Nodes = append(out.Nodes, *row)
	}
	writeJSON(w, out)
}

func (s *Server) nodeRow(ctx context.Context, ref string, l *LineInfo, station string,
	from, to time.Time, ranges [][2]time.Time, prod []compose.Span) (*NodeCompareRow, error) {

	row := &NodeCompareRow{Ref: ref, Line: l.Name, Causes: []causeRow{},
		Flow: []FlowState{}, FlowReasons: []FlowReason{}}

	var res compose.Result
	var ems []EMInfo
	if station == "" {
		row.Kind, row.Display = "line", l.Display
		r, _, isDefault, _, err := s.evalLineComposed(ctx, l, from, to)
		if err != nil {
			return nil, err
		}
		res, row.DefaultModel = r, isDefault
		ems = l.EMs()
	} else {
		st := s.findStation(l.Name, station)
		if st == nil {
			return nil, nil
		}
		row.Kind, row.Station, row.Display = "station", st.Name, st.Display
		r, _, isDefault, err := s.composeStation(ctx, l, st, from, to)
		if err != nil {
			return nil, err
		}
		res, row.DefaultModel = r, isDefault
		ems = st.EMs
	}
	row.EMCount = len(ems)

	var prodMs int64
	for _, p := range prod {
		prodMs += p.End - p.Start
	}
	row.ProductionMin = round1(float64(prodMs) / 60000.0)
	if prodMs > 0 && len(ems) > 0 {
		pct := round1(100 * float64(res.UpMs(prod)) / float64(prodMs))
		row.AvailabilityPct = &pct
	}
	row.DownMin = round1(float64(res.DownMs(prod)) / 60000.0)
	for name, ms := range res.CausePareto(prod) {
		row.Causes = append(row.Causes, causeRow{name, round1(float64(ms) / 60000.0)})
	}
	sort.Slice(row.Causes, func(i, j int) bool {
		return row.Causes[i].Minutes > row.Causes[j].Minutes
	})
	if len(row.Causes) > 8 {
		row.Causes = row.Causes[:8]
	}

	if len(ems) == 0 {
		return row, nil
	}
	ids := make([]int, 0, len(ems))
	for _, e := range ems {
		ids = append(ids, e.ID)
	}
	if err := s.nodeFlow(ctx, ids, ranges, from, to, row); err != nil {
		return nil, err
	}
	return row, nil
}

// nodeFlow fills in the flow-loss totals and reason pareto for one node,
// clipped to production time so it lines up with the availability number.
func (s *Server) nodeFlow(ctx context.Context, ids []int, ranges [][2]time.Time,
	from, to time.Time, row *NodeCompareRow) error {

	rows, err := s.pool.Query(ctx, `
	    SELECT state, COALESCE(reason,''), start_ts, end_ts
	    FROM state_interval
	    WHERE em_id = ANY($1) AND state = ANY($2)
	      AND end_ts > $3 AND start_ts < $4`, ids, flowStates, from, to)
	if err != nil {
		return err
	}
	defer rows.Close()

	emMs := map[string]int64{}
	counts := map[string]int{}
	spans := map[string][][2]time.Time{}
	type rk struct{ state, reason string }
	byReason := map[rk]*FlowReason{}

	for rows.Next() {
		var state, reason string
		var s0, e0 time.Time
		if err := rows.Scan(&state, &reason, &s0, &e0); err != nil {
			return err
		}
		ms := intersectMs(s0, e0, ranges)
		if ms == 0 {
			continue // entirely outside production time
		}
		emMs[state] += ms
		counts[state]++
		spans[state] = append(spans[state], [2]time.Time{s0, e0})
		if reason == "" {
			reason = "(no reason reported)"
		}
		k := rk{state, reason}
		if byReason[k] == nil {
			byReason[k] = &FlowReason{Reason: reason, State: state}
		}
		byReason[k].Minutes += float64(ms) / 60000.0
		byReason[k].Count++
	}
	if err := rows.Err(); err != nil {
		return err
	}

	for _, state := range flowStates {
		if counts[state] == 0 {
			continue
		}
		row.Flow = append(row.Flow, FlowState{
			State:   state,
			EMMin:   round1(float64(emMs[state]) / 60000.0),
			WallMin: round1(float64(unionMs(spans[state], ranges)) / 60000.0),
			Count:   counts[state],
		})
	}
	for _, fr := range byReason {
		fr.Minutes = round1(fr.Minutes)
		row.FlowReasons = append(row.FlowReasons, *fr)
	}
	sort.Slice(row.FlowReasons, func(i, j int) bool {
		return row.FlowReasons[i].Minutes > row.FlowReasons[j].Minutes
	})
	if len(row.FlowReasons) > 12 {
		row.FlowReasons = row.FlowReasons[:12]
	}
	return nil
}

// unionMs is the clock time covered by at least one of `spans`, clipped to
// `ranges`. Overlapping intervals from different EMs collapse into one, which
// is the whole point: a line-wide starve is one outage, not N.
func unionMs(spans [][2]time.Time, ranges [][2]time.Time) int64 {
	if len(spans) == 0 {
		return 0
	}
	sorted := make([][2]time.Time, len(spans))
	copy(sorted, spans)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i][0].Before(sorted[j][0]) })

	var total int64
	cur := sorted[0]
	for _, sp := range sorted[1:] {
		if sp[0].After(cur[1]) { // disjoint — bank the run so far
			total += intersectMs(cur[0], cur[1], ranges)
			cur = sp
			continue
		}
		if sp[1].After(cur[1]) {
			cur[1] = sp[1]
		}
	}
	return total + intersectMs(cur[0], cur[1], ranges)
}
