package api

// Composed (k-of-n) availability: stations compose their EMs, lines compose
// their stations. Trees are stored as JSONB on station/line rows (NULL =
// default: ALL members in series). Evaluation is time-domain — see the
// compose package for why percentages must never be multiplied.

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"sort"
	"strings"
	"time"

	"github.com/mike-at-base/equipment_monitor/hub/internal/compose"
)

// ── up spans per EM ──────────────────────────────────────────────────────

// emUpSpans returns, per EM id, the merged spans where the EM counted as
// available (state in availStates). Everything else — down, manual, offline,
// gaps with no data at all — is composition-down by omission.
func (s *Server) emUpSpans(ctx context.Context, ids []int, from, to time.Time) (map[int][]compose.Span, error) {
	raw := map[int][]compose.Span{}
	rows, err := s.pool.Query(ctx, `
	    SELECT em_id, state, start_ts, end_ts FROM state_interval
	    WHERE em_id = ANY($1) AND end_ts > $2 AND start_ts < $3`, ids, from, to)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var id int
		var st string
		var s0, e0 time.Time
		if err := rows.Scan(&id, &st, &s0, &e0); err != nil {
			return nil, err
		}
		if !strings.Contains(availStates, st) {
			continue
		}
		raw[id] = append(raw[id], clampSpan(s0, e0, from, to))
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	// current open interval (in memory, not yet flushed to the DB)
	now := time.Now().UTC()
	idSet := map[int]bool{}
	for _, id := range ids {
		idSet[id] = true
	}
	for _, le := range s.live() {
		if !idSet[le.EMID] || le.Since.IsZero() || !strings.Contains(availStates, le.State) {
			continue
		}
		if le.Since.Before(to) && now.After(from) {
			raw[le.EMID] = append(raw[le.EMID], clampSpan(le.Since, now, from, to))
		}
	}
	for id := range raw {
		raw[id] = mergeSpans(raw[id])
	}
	return raw, nil
}

func clampSpan(s0, e0, from, to time.Time) compose.Span {
	if s0.Before(from) {
		s0 = from
	}
	if e0.After(to) {
		e0 = to
	}
	return compose.Span{Start: s0.UnixMilli(), End: e0.UnixMilli()}
}

func mergeSpans(spans []compose.Span) []compose.Span {
	sort.Slice(spans, func(i, j int) bool { return spans[i].Start < spans[j].Start })
	out := spans[:0]
	for _, sp := range spans {
		if sp.End <= sp.Start {
			continue
		}
		if n := len(out); n > 0 && sp.Start <= out[n-1].End {
			if sp.End > out[n-1].End {
				out[n-1].End = sp.End
			}
			continue
		}
		out = append(out, sp)
	}
	return out
}

func spansFromRanges(ranges [][2]time.Time) []compose.Span {
	out := make([]compose.Span, 0, len(ranges))
	for _, r := range ranges {
		out = append(out, compose.Span{Start: r[0].UnixMilli(), End: r[1].UnixMilli()})
	}
	return out
}

// ── model storage ────────────────────────────────────────────────────────

func (s *Server) loadStationModel(ctx context.Context, line, station string) (*compose.Node, error) {
	var raw []byte
	err := s.pool.QueryRow(ctx, `
	    SELECT st.avail_model FROM station st JOIN line l ON l.id = st.line_id
	    WHERE lower(l.name) = lower($1) AND lower(st.name) = lower($2)`,
		line, station).Scan(&raw)
	if err != nil || raw == nil {
		return nil, err
	}
	var n compose.Node
	if err := json.Unmarshal(raw, &n); err != nil {
		return nil, err
	}
	return &n, nil
}

func (s *Server) loadLineModel(ctx context.Context, line string) (*compose.Node, error) {
	var raw []byte
	err := s.pool.QueryRow(ctx,
		`SELECT avail_model FROM line WHERE lower(name) = lower($1)`, line).Scan(&raw)
	if err != nil || raw == nil {
		return nil, err
	}
	var n compose.Node
	if err := json.Unmarshal(raw, &n); err != nil {
		return nil, err
	}
	return &n, nil
}

// defaultStationModel = ALL of the station's EMs in series.
func defaultStationModel(st *StationInfo) *compose.Node {
	n := &compose.Node{K: compose.K{All: true}}
	for i := range st.EMs {
		n.Children = append(n.Children, compose.Node{EM: st.EMs[i].Label})
	}
	return n
}

// defaultLineModel = ALL of the line's stations in series.
func defaultLineModel(l *LineInfo) *compose.Node {
	n := &compose.Node{K: compose.K{All: true}}
	for i := range l.Stations {
		n.Children = append(n.Children, compose.Node{Station: l.Stations[i].Name})
	}
	return n
}

// ── evaluation ───────────────────────────────────────────────────────────

// composeStation evaluates one station's tree over [from,to]. Returns the
// result, the tree used, and whether it was the default.
func (s *Server) composeStation(ctx context.Context, l *LineInfo, st *StationInfo,
	from, to time.Time) (compose.Result, *compose.Node, bool, error) {
	node, err := s.loadStationModel(ctx, l.Name, st.Name)
	if err != nil {
		return compose.Result{}, nil, false, err
	}
	isDefault := node == nil
	if isDefault {
		node = defaultStationModel(st)
	}
	if len(st.EMs) == 0 {
		return compose.Result{}, node, isDefault, nil
	}
	idByLabel := map[string]int{}
	ids := make([]int, 0, len(st.EMs))
	for i := range st.EMs {
		idByLabel[strings.ToLower(st.EMs[i].Label)] = st.EMs[i].ID
		ids = append(ids, st.EMs[i].ID)
	}
	byID, err := s.emUpSpans(ctx, ids, from, to)
	if err != nil {
		return compose.Result{}, nil, false, err
	}
	ups := map[string][]compose.Span{}
	for _, leafKey := range node.Leaves() {
		if id, ok := idByLabel[strings.ToLower(leafKey)]; ok {
			ups[leafKey] = byID[id]
		}
		// unknown leaf (EM deleted since the model was saved) stays absent
		// from ups -> counts as down; surfaced by validation on next save.
	}
	return compose.Eval(node, ups, from.UnixMilli(), to.UnixMilli()), node, isDefault, nil
}

type composedDTO struct {
	Pct           *float64        `json:"pct"`
	UpSpans       []compose.Span  `json:"up_spans"`
	Down          []composeDown   `json:"down"`
	Causes        []causeRow      `json:"causes"`
	Default       bool            `json:"default_model"`
	Model         *compose.Node   `json:"model"`
	ProductionMin float64         `json:"production_min"`
}

type composeDown struct {
	Start  time.Time `json:"start_ts"`
	End    time.Time `json:"end_ts"`
	Causes []string  `json:"causes"`
}

type causeRow struct {
	Name    string  `json:"name"`
	Minutes float64 `json:"minutes"`
}

func buildComposedDTO(res compose.Result, node *compose.Node, isDefault bool,
	prod []compose.Span) composedDTO {
	// Normalize nil to empty so JSON encodes "up_spans":[] — a nil slice
	// marshals as null and the SPA crashes on up_spans.map(...).
	up := res.Up
	if up == nil {
		up = []compose.Span{}
	}
	dto := composedDTO{
		UpSpans: up, Default: isDefault, Model: node,
		Down: []composeDown{}, Causes: []causeRow{},
	}
	var prodMs int64
	for _, p := range prod {
		prodMs += p.End - p.Start
	}
	dto.ProductionMin = round1(float64(prodMs) / 60000.0)
	if prodMs > 0 {
		pct := round1(100 * float64(res.UpMs(prod)) / float64(prodMs))
		dto.Pct = &pct
	}
	for _, d := range res.Down {
		dto.Down = append(dto.Down, composeDown{
			Start: time.UnixMilli(d.Start).UTC(), End: time.UnixMilli(d.End).UTC(),
			Causes: d.Causes,
		})
	}
	pareto := res.CausePareto(prod)
	for name, ms := range pareto {
		dto.Causes = append(dto.Causes, causeRow{name, round1(float64(ms) / 60000.0)})
	}
	sort.Slice(dto.Causes, func(i, j int) bool { return dto.Causes[i].Minutes > dto.Causes[j].Minutes })
	return dto
}

// ── handlers: composed results ───────────────────────────────────────────

func (s *Server) handleStationComposed(w http.ResponseWriter, r *http.Request) {
	l, st, ok := s.stationOr404(w, r)
	if !ok {
		return
	}
	from, to, err := s.window(r)
	if err != nil {
		httpErr(w, 400, err)
		return
	}
	ranges, err := s.lineProductionRanges(r.Context(), l.Name, from, to)
	if err != nil {
		httpErr(w, 500, err)
		return
	}
	res, node, isDefault, err := s.composeStation(r.Context(), l, st, from, to)
	if err != nil {
		httpErr(w, 500, err)
		return
	}
	dto := buildComposedDTO(res, node, isDefault, spansFromRanges(ranges))
	writeJSON(w, map[string]any{
		"from": from, "to": to, "station": st.Name,
		"composed": dto,
	})
}

// evalLineComposed evaluates every station, then the line tree. stationUps
// maps station name → that station's composed-up spans (line leaf inputs).
func (s *Server) evalLineComposed(ctx context.Context, l *LineInfo, from, to time.Time) (
	compose.Result, *compose.Node, bool, map[string][]compose.Span, error) {
	stationUps := map[string][]compose.Span{}
	for i := range l.Stations {
		st := &l.Stations[i]
		res, _, _, err := s.composeStation(ctx, l, st, from, to)
		if err != nil {
			return compose.Result{}, nil, false, nil, err
		}
		stationUps[st.Name] = res.Up
	}
	node, err := s.loadLineModel(ctx, l.Name)
	if err != nil {
		return compose.Result{}, nil, false, nil, err
	}
	isDefault := node == nil
	if isDefault {
		node = defaultLineModel(l)
	}
	res := compose.Eval(node, stationUps, from.UnixMilli(), to.UnixMilli())
	return res, node, isDefault, stationUps, nil
}

func (s *Server) handleLineComposed(w http.ResponseWriter, r *http.Request) {
	l := s.findLine(r.PathValue("line"))
	if l == nil {
		httpErr(w, 404, fmt.Errorf("unknown line"))
		return
	}
	from, to, err := s.window(r)
	if err != nil {
		httpErr(w, 400, err)
		return
	}
	ranges, err := s.lineProductionRanges(r.Context(), l.Name, from, to)
	if err != nil {
		httpErr(w, 500, err)
		return
	}
	prod := spansFromRanges(ranges)

	res, node, isDefault, stationUps, err := s.evalLineComposed(r.Context(), l, from, to)
	if err != nil {
		httpErr(w, 500, err)
		return
	}
	var prodMs int64
	for _, p := range prod {
		prodMs += p.End - p.Start
	}
	stationPct := map[string]*float64{}
	for name, ups := range stationUps {
		stRes := compose.Result{Up: ups}
		if prodMs > 0 {
			pct := round1(100 * float64(stRes.UpMs(prod)) / float64(prodMs))
			stationPct[name] = &pct
		} else {
			stationPct[name] = nil
		}
	}
	dto := buildComposedDTO(res, node, isDefault, prod)
	writeJSON(w, map[string]any{
		"from": from, "to": to, "line": l.Name,
		"composed": dto,
		"stations": stationPct,
	})
}

// ── handlers: model get/save ─────────────────────────────────────────────

func (s *Server) stationOr404(w http.ResponseWriter, r *http.Request) (*LineInfo, *StationInfo, bool) {
	l := s.findLine(r.PathValue("line"))
	if l == nil {
		httpErr(w, 404, fmt.Errorf("unknown line"))
		return nil, nil, false
	}
	name := r.PathValue("station")
	for i := range l.Stations {
		if strings.EqualFold(l.Stations[i].Name, name) {
			return l, &l.Stations[i], true
		}
	}
	httpErr(w, 404, fmt.Errorf("unknown station"))
	return nil, nil, false
}

func (s *Server) handleGetStationModel(w http.ResponseWriter, r *http.Request) {
	l, st, ok := s.stationOr404(w, r)
	if !ok {
		return
	}
	node, err := s.loadStationModel(r.Context(), l.Name, st.Name)
	if err != nil {
		httpErr(w, 500, err)
		return
	}
	members := make([]string, 0, len(st.EMs))
	for i := range st.EMs {
		members = append(members, st.EMs[i].Label)
	}
	writeJSON(w, map[string]any{
		"model": node, "default_model": defaultStationModel(st), "members": members,
	})
}

func (s *Server) handleSaveStationModel(w http.ResponseWriter, r *http.Request) {
	l, st, ok := s.stationOr404(w, r)
	if !ok {
		return
	}
	allowed := map[string]bool{}
	for i := range st.EMs {
		allowed[st.EMs[i].Label] = true
	}
	s.saveModel(w, r, allowed, `
	    UPDATE station SET avail_model = $3 FROM line
	    WHERE station.line_id = line.id
	      AND lower(line.name) = lower($1) AND lower(station.name) = lower($2)`,
		l.Name, st.Name)
}

func (s *Server) handleGetLineModel(w http.ResponseWriter, r *http.Request) {
	l := s.findLine(r.PathValue("line"))
	if l == nil {
		httpErr(w, 404, fmt.Errorf("unknown line"))
		return
	}
	node, err := s.loadLineModel(r.Context(), l.Name)
	if err != nil {
		httpErr(w, 500, err)
		return
	}
	members := make([]string, 0, len(l.Stations))
	for i := range l.Stations {
		members = append(members, l.Stations[i].Name)
	}
	writeJSON(w, map[string]any{
		"model": node, "default_model": defaultLineModel(l), "members": members,
	})
}

func (s *Server) handleSaveLineModel(w http.ResponseWriter, r *http.Request) {
	l := s.findLine(r.PathValue("line"))
	if l == nil {
		httpErr(w, 404, fmt.Errorf("unknown line"))
		return
	}
	allowed := map[string]bool{}
	for i := range l.Stations {
		allowed[l.Stations[i].Name] = true
	}
	s.saveModel(w, r, allowed,
		`UPDATE line SET avail_model = $2 WHERE lower(name) = lower($1)`, l.Name)
}

// saveModel parses {"model": <tree|null>}, validates against the allowed
// member set, and runs the given UPDATE with the JSON (or NULL to clear).
func (s *Server) saveModel(w http.ResponseWriter, r *http.Request,
	allowed map[string]bool, query string, args ...any) {
	var body struct {
		Model *compose.Node `json:"model"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		httpErr(w, 400, err)
		return
	}
	var val any // nil clears back to the default model
	if body.Model != nil {
		if err := body.Model.Validate(allowed); err != nil {
			httpErr(w, 400, err)
			return
		}
		raw, err := json.Marshal(body.Model)
		if err != nil {
			httpErr(w, 500, err)
			return
		}
		val = raw
	}
	tag, err := s.pool.Exec(r.Context(), query, append(args, val)...)
	if err != nil {
		httpErr(w, 500, err)
		return
	}
	if tag.RowsAffected() == 0 {
		httpErr(w, 404, fmt.Errorf("row not found"))
		return
	}
	writeJSON(w, map[string]any{"ok": true})
}
