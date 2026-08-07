package api

// Utilization: of the time this equipment had, how much was it actually
// making product?
//
// SEMI E10 puts productive time over total time, and everything that is not
// productive — starved, blocked, non-value-added, standby, down — counts
// against it. That makes it a different question from availability, which
// asks only whether the equipment COULD run: a module that is up all shift
// but starved half of it is ~100% available and ~50% utilized, and the gap
// between the two numbers is the flow loss.
//
// Two denominators, because they answer different things and disagree by a
// lot on a scheduled line:
//
//   pct        over production time — comparable with the availability
//              number beside it, since that is production-clipped too
//   window_pct over the whole requested window — E10's Total Time, which
//              charges nights and weekends against the machine
//
// On an unscheduled line production time IS the window and the two converge,
// which is why `scheduled` is reported rather than left to be inferred.

import (
	"context"
	"time"
)

type Utilization struct {
	// productive / production time
	Pct           *float64 `json:"pct"`
	ProductiveMin float64  `json:"productive_min"`
	ProductionMin float64  `json:"production_min"`
	Scheduled     bool     `json:"scheduled"`

	// productive / the whole window (E10 Total Time)
	WindowPct           *float64 `json:"window_pct"`
	WindowProductiveMin float64  `json:"window_productive_min"`
	WindowMin           float64  `json:"window_min"`
}

// productiveState is the only state that counts toward utilization. Every
// other availStates member is uptime that is not making anything.
const productiveState = "productive"

// utilization measures productive time twice: once clipped to the line's
// production ranges, once across the whole window.
func (s *Server) utilization(ctx context.Context, emID int, from, to time.Time,
	ranges [][2]time.Time, scheduled bool) (*Utilization, error) {

	rows, err := s.pool.Query(ctx, `
	    SELECT start_ts, end_ts FROM state_interval
	    WHERE em_id = $1 AND state = $2 AND end_ts > $3 AND start_ts < $4`,
		emID, productiveState, from, to)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var prodMs, winMs int64
	for rows.Next() {
		var s0, e0 time.Time
		if err := rows.Scan(&s0, &e0); err != nil {
			return nil, err
		}
		prodMs += intersectMs(s0, e0, ranges)
		winMs += overlapMs(s0, e0, from, to)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}

	// the current state has no closed interval yet; without it a machine
	// running right now reads as idle for the newest slice of the window
	now := time.Now().UTC()
	for _, le := range s.live() {
		if le.EMID != emID || le.State != productiveState || le.Since.IsZero() {
			continue
		}
		prodMs += intersectMs(le.Since, now, ranges)
		winMs += overlapMs(le.Since, now, from, to)
	}

	u := &Utilization{
		ProductiveMin:       round1(float64(prodMs) / 60000.0),
		ProductionMin:       round1(float64(rangesMs(ranges)) / 60000.0),
		Scheduled:           scheduled,
		WindowProductiveMin: round1(float64(winMs) / 60000.0),
		WindowMin:           round1(to.Sub(from).Minutes()),
	}
	if d := rangesMs(ranges); d > 0 {
		p := round1(100 * float64(prodMs) / float64(d))
		u.Pct = &p
	}
	if d := to.Sub(from).Milliseconds(); d > 0 {
		p := round1(100 * float64(winMs) / float64(d))
		u.WindowPct = &p
	}
	return u, nil
}

// lineIsScheduled reports whether the line has any shifts configured, so the
// UI can say whether production time is a real constraint or just the window.
func (s *Server) lineIsScheduled(ctx context.Context, line string) (bool, error) {
	shifts, err := s.lineShifts(ctx, line)
	if err != nil {
		return false, err
	}
	return len(shifts) > 0, nil
}
