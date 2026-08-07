package api

// Time by state, bucketed — the stacked-column view of how a shift actually
// went, rather than a single total for the window.
//
// Intervals are split across bucket boundaries in SQL rather than assigned
// whole to the bucket they start in. A forty-minute down that begins at 09:55
// belongs five minutes to the 09:00 hour and thirty-five to the 10:00 hour;
// charging it all to the first is how a chart ends up disagreeing with the
// totals beside it.

import (
	"context"
	"net/http"
	"time"
)

// columnTargetBuckets is what auto-sizing aims for. Columns need to stay wide
// enough to read, so this is far coarser than the drift line chart.
const columnTargetBuckets = 32

// In split mode every slice holds one column per EM, so the budget is on the
// total number of bars rather than the number of slices. Each bar also
// carries a rotated label naming its module, which sets a floor on how
// narrow a bar can usefully get — hence the modest budget.
const (
	splitTargetColumns = 60
	minSplitBuckets    = 5
)

type StateStack struct {
	Bucket  string      `json:"bucket"`
	From    time.Time   `json:"from"`
	To      time.Time   `json:"to"`
	Buckets []time.Time `json:"buckets"`
	// one entry per state present, each with a value per bucket, in minutes.
	// Summed across the selected EMs, so a bucket can hold more minutes than
	// it has wall clock.
	Series []StateSeries `json:"series"`
	// With split=em the same numbers arrive broken out per EM instead, for a
	// column per module within each time slice. Series is then empty.
	Groups  []StateGroup `json:"groups,omitempty"`
	EMCount int          `json:"em_count"`
}

type StateGroup struct {
	Ref     string        `json:"ref"`
	Station string        `json:"station"`
	EMLabel string        `json:"em_label"`
	Display string        `json:"display_name"`
	Series  []StateSeries `json:"series"`
}

type StateSeries struct {
	State   string    `json:"state"`
	Minutes []float64 `json:"minutes"`
}

// handleStateStack serves GET /api/v2/statestack?ems=...&bucket=auto
func (s *Server) handleStateStack(w http.ResponseWriter, r *http.Request) {
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
	split := r.URL.Query().Get("split") == "em"
	var ids []int
	byID := map[int]EMCompareRow{}
	for _, ref := range refs {
		line, station, label, ok := splitEMRef(ref)
		if !ok {
			continue
		}
		if _, em := s.findEM(line, station, label); em != nil {
			if _, dup := byID[em.ID]; dup {
				continue
			}
			ids = append(ids, em.ID)
			byID[em.ID] = EMCompareRow{Ref: ref, Station: em.Station,
				EMLabel: em.Label, Display: em.Display}
		}
	}

	// Split mode draws EMCount columns per slice, so the same number of
	// buckets would give EMCount times as many bars. Coarsen to keep the
	// total readable rather than rendering slivers.
	target := columnTargetBuckets
	if split && len(ids) > 1 {
		target = splitTargetColumns / len(ids)
		if target < minSplitBuckets {
			target = minSplitBuckets
		}
		if target > columnTargetBuckets {
			target = columnTargetBuckets
		}
	}
	name, bucket := columnBucket(r.URL.Query().Get("bucket"), from, to, target)
	out := StateStack{Bucket: name, From: from, To: to, EMCount: len(ids),
		Buckets: []time.Time{}, Series: []StateSeries{}}
	if len(ids) == 0 {
		writeJSON(w, out)
		return
	}
	var err2 error
	if split {
		err2 = s.fillStateStackSplit(r.Context(), ids, byID, from, to, bucket, &out)
	} else {
		err2 = s.fillStateStack(r.Context(), ids, from, to, bucket, &out)
	}
	if err2 != nil {
		httpErr(w, 500, err2)
		return
	}
	writeJSON(w, out)
}

// columnBucket resolves an explicit bucket name, or picks one aiming for
// roughly columnTargetBuckets columns.
func columnBucket(want string, from, to time.Time, targetBuckets int) (string, time.Duration) {
	if d, ok := bucketDurations[want]; ok {
		return want, d
	}
	span := to.Sub(from)
	if span <= 0 {
		return "1h", time.Hour
	}
	if targetBuckets < 1 {
		targetBuckets = columnTargetBuckets
	}
	target := span / time.Duration(targetBuckets)
	for _, b := range niceBuckets {
		if b.d >= target {
			return b.name, b.d
		}
	}
	last := niceBuckets[len(niceBuckets)-1]
	return last.name, last.d
}

// stateByEM is the shared core: minutes per EM, per state, per bucket. Both
// the combined and the split view are shaped from this, so the interval
// splitting and the live-interval handling exist once.
func (s *Server) stateByEM(ctx context.Context, ids []int, from, to time.Time,
	bucket time.Duration, buckets []time.Time) (map[int]map[string][]float64, error) {

	index := map[time.Time]int{}
	for i, t := range buckets {
		index[t] = i
	}
	out := map[int]map[string][]float64{}
	put := func(id int, state string, i int, mins float64) {
		if out[id] == nil {
			out[id] = map[string][]float64{}
		}
		if out[id][state] == nil {
			out[id][state] = make([]float64, len(buckets))
		}
		out[id][state][i] += mins
	}

	start, last := buckets[0], buckets[len(buckets)-1]
	// generate_series gives one row per bucket; the join clips each interval
	// to the bucket it overlaps, so an interval spanning several is split
	// between them rather than landing wholly in the first.
	//
	// The window guard in the WHERE clause is load-bearing. The first bucket
	// starts at or before `from`, so without it an interval that ended
	// between the two still joins, clips to a NEGATIVE duration, and quietly
	// subtracts time from the column. Same at the far end.
	rows, err := s.pool.Query(ctx, `
	    SELECT b.bucket, si.em_id, si.state,
	           SUM(EXTRACT(EPOCH FROM (
	               LEAST(si.end_ts, b.bucket + $5, $4::timestamptz)
	             - GREATEST(si.start_ts, b.bucket, $3::timestamptz))))/60.0
	    FROM generate_series($1::timestamptz, $2::timestamptz, $5) AS b(bucket)
	    JOIN state_interval si
	      ON si.end_ts > b.bucket AND si.start_ts < b.bucket + $5
	    WHERE si.em_id = ANY($6)
	      AND si.end_ts > $3::timestamptz AND si.start_ts < $4::timestamptz
	    GROUP BY b.bucket, si.em_id, si.state`,
		start, last, from, to, bucket, ids)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var t time.Time
		var id int
		var state string
		var mins float64
		if err := rows.Scan(&t, &id, &state, &mins); err != nil {
			return nil, err
		}
		if i, ok := index[t.UTC()]; ok {
			put(id, state, i, round1(mins))
		}
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}

	// The state an EM is in right now has no row in state_interval yet, so on
	// a live line the newest column would read empty without this.
	idSet := map[int]bool{}
	for _, id := range ids {
		idSet[id] = true
	}
	now := time.Now().UTC()
	for _, le := range s.live() {
		if !idSet[le.EMID] || le.State == "" || le.Since.IsZero() {
			continue
		}
		for i, b := range buckets {
			lo, hi := maxTime(b, le.Since, from), minTime(b.Add(bucket), now, to)
			if hi.After(lo) {
				put(le.EMID, le.State, i, round1(hi.Sub(lo).Minutes()))
			}
		}
	}
	return out, nil
}

func bucketList(from, to time.Time, bucket time.Duration) []time.Time {
	start, last := bucketRange(from, to, bucket)
	var out []time.Time
	for t := start; !t.After(last); t = t.Add(bucket) {
		out = append(out, t)
	}
	return out
}

// orderedSeries turns a state->values map into the stable stacking order.
func orderedSeries(byState map[string][]float64) []StateSeries {
	out := []StateSeries{}
	seen := map[string]bool{}
	for _, st := range stateStackOrder {
		if v, ok := byState[st]; ok {
			out = append(out, StateSeries{State: st, Minutes: v})
			seen[st] = true
		}
	}
	for st, v := range byState { // anything unexpected, still shown
		if !seen[st] {
			out = append(out, StateSeries{State: st, Minutes: v})
		}
	}
	return out
}

func (s *Server) fillStateStack(ctx context.Context, ids []int, from, to time.Time,
	bucket time.Duration, out *StateStack) error {

	out.Buckets = bucketList(from, to, bucket)
	byEM, err := s.stateByEM(ctx, ids, from, to, bucket, out.Buckets)
	if err != nil {
		return err
	}
	summed := map[string][]float64{}
	for _, byState := range byEM {
		for st, vals := range byState {
			if summed[st] == nil {
				summed[st] = make([]float64, len(out.Buckets))
			}
			for i, v := range vals {
				summed[st][i] += v
			}
		}
	}
	out.Series = orderedSeries(summed)
	return nil
}

// fillStateStackSplit keeps the EMs apart: one column per module within each
// time slice, in the order the dashboard listed them.
func (s *Server) fillStateStackSplit(ctx context.Context, ids []int,
	byID map[int]EMCompareRow, from, to time.Time, bucket time.Duration,
	out *StateStack) error {

	out.Buckets = bucketList(from, to, bucket)
	byEM, err := s.stateByEM(ctx, ids, from, to, bucket, out.Buckets)
	if err != nil {
		return err
	}
	out.Groups = []StateGroup{}
	for _, id := range ids { // selection order, not map order
		info := byID[id]
		out.Groups = append(out.Groups, StateGroup{
			Ref: info.Ref, Station: info.Station, EMLabel: info.EMLabel,
			Display: info.Display, Series: orderedSeries(byEM[id]),
		})
	}
	return nil
}

// good at the bottom of the stack, bad at the top, so the eye reads the
// productive band as a baseline that the losses sit on
var stateStackOrder = []string{
	"productive", "nva", "standby", "process_wait", "wait",
	"starved", "blocked", "paused", "manual", "offline", "down",
}

// maxTime / minTime clip a bucket to both the span being charted and the
// interval being placed in it.
func maxTime(ts ...time.Time) time.Time {
	out := ts[0]
	for _, t := range ts[1:] {
		if t.After(out) {
			out = t
		}
	}
	return out
}

func minTime(ts ...time.Time) time.Time {
	out := ts[0]
	for _, t := range ts[1:] {
		if t.Before(out) {
			out = t
		}
	}
	return out
}
