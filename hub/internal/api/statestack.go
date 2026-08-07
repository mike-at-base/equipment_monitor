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

type StateStack struct {
	Bucket  string      `json:"bucket"`
	From    time.Time   `json:"from"`
	To      time.Time   `json:"to"`
	Buckets []time.Time `json:"buckets"`
	// one entry per state present, each with a value per bucket, in minutes
	Series []StateSeries `json:"series"`
	// EM-minutes: with several EMs selected the states are summed, so a
	// bucket can hold more minutes than it has wall clock
	EMCount int `json:"em_count"`
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
	var ids []int
	for _, ref := range refs {
		line, station, label, ok := splitEMRef(ref)
		if !ok {
			continue
		}
		if _, em := s.findEM(line, station, label); em != nil {
			ids = append(ids, em.ID)
		}
	}

	name, bucket := columnBucket(r.URL.Query().Get("bucket"), from, to)
	out := StateStack{Bucket: name, From: from, To: to, EMCount: len(ids),
		Buckets: []time.Time{}, Series: []StateSeries{}}
	if len(ids) == 0 {
		writeJSON(w, out)
		return
	}
	if err := s.fillStateStack(r.Context(), ids, from, to, bucket, &out); err != nil {
		httpErr(w, 500, err)
		return
	}
	writeJSON(w, out)
}

// columnBucket resolves an explicit bucket name, or picks one aiming for
// roughly columnTargetBuckets columns.
func columnBucket(want string, from, to time.Time) (string, time.Duration) {
	if d, ok := bucketDurations[want]; ok {
		return want, d
	}
	span := to.Sub(from)
	if span <= 0 {
		return "1h", time.Hour
	}
	target := span / columnTargetBuckets
	for _, b := range niceBuckets {
		if b.d >= target {
			return b.name, b.d
		}
	}
	last := niceBuckets[len(niceBuckets)-1]
	return last.name, last.d
}

func (s *Server) fillStateStack(ctx context.Context, ids []int, from, to time.Time,
	bucket time.Duration, out *StateStack) error {

	start, last := bucketRange(from, to, bucket)
	for t := start; !t.After(last); t = t.Add(bucket) {
		out.Buckets = append(out.Buckets, t)
	}
	index := map[time.Time]int{}
	for i, t := range out.Buckets {
		index[t] = i
	}

	// generate_series gives one row per bucket; the join clips each interval
	// to the bucket it overlaps, so an interval spanning several is split
	// between them rather than landing wholly in the first.
	//
	// The window guard in the WHERE clause is load-bearing. The first bucket
	// starts at or before `from`, so without it an interval that ended
	// between the two still joins, clips to a NEGATIVE duration, and quietly
	// subtracts time from the column. Same at the far end.
	rows, err := s.pool.Query(ctx, `
	    SELECT b.bucket, si.state,
	           SUM(EXTRACT(EPOCH FROM (
	               LEAST(si.end_ts, b.bucket + $5, $4::timestamptz)
	             - GREATEST(si.start_ts, b.bucket, $3::timestamptz))))/60.0
	    FROM generate_series($1::timestamptz, $2::timestamptz, $5) AS b(bucket)
	    JOIN state_interval si
	      ON si.end_ts > b.bucket AND si.start_ts < b.bucket + $5
	    WHERE si.em_id = ANY($6)
	      AND si.end_ts > $3::timestamptz AND si.start_ts < $4::timestamptz
	    GROUP BY b.bucket, si.state`,
		start, last, from, to, bucket, ids)
	if err != nil {
		return err
	}
	defer rows.Close()

	byState := map[string][]float64{}
	for rows.Next() {
		var t time.Time
		var state string
		var mins float64
		if err := rows.Scan(&t, &state, &mins); err != nil {
			return err
		}
		i, ok := index[t.UTC()]
		if !ok {
			continue
		}
		if byState[state] == nil {
			byState[state] = make([]float64, len(out.Buckets))
		}
		byState[state][i] += round1(mins)
	}
	if err := rows.Err(); err != nil {
		return err
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
		for i, b := range out.Buckets {
			lo, hi := maxTime(b, le.Since, from), minTime(b.Add(bucket), now, to)
			if !hi.After(lo) {
				continue
			}
			if byState[le.State] == nil {
				byState[le.State] = make([]float64, len(out.Buckets))
			}
			byState[le.State][i] += round1(hi.Sub(lo).Minutes())
		}
	}

	// stateStackOrder keeps the stack in a stable, meaningful order rather
	// than whatever the map iteration produced
	for _, st := range stateStackOrder {
		if v, ok := byState[st]; ok {
			out.Series = append(out.Series, StateSeries{State: st, Minutes: v})
			delete(byState, st)
		}
	}
	for st, v := range byState { // anything unexpected, still shown
		out.Series = append(out.Series, StateSeries{State: st, Minutes: v})
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
