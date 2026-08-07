package api

// User-composed dashboards: a saved list of widgets, each pointed at an
// entity of the caller's choosing.
//
// Two things are deliberately NOT here:
//
//   - Ownership. This service has no auth of any kind, so a dashboard cannot
//     belong to anyone. `author` is a free-text label the user types; it is
//     never checked. Adding an owner column would imply an access control
//     that does not exist.
//   - The time window. It comes from the UI's global picker, which already
//     lives in the URL, so a shared dashboard link carries the range with it.

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"regexp"
	"strings"
	"time"
)

// widgetScopeKinds are the entity shapes a widget can be pointed at.
const (
	scopeNone    = "none"
	scopeLine    = "line"
	scopeStation = "station"
	scopeEM      = "em"
	scopeEMs     = "ems"
)

// widgetTypes maps each known widget to the scope kinds it accepts. The
// frontend registry is the source of truth for rendering; this is the
// server-side guard so a malformed spec cannot be persisted.
var widgetTypes = map[string][]string{
	// multi-EM comparisons
	"cycle_compare":        {scopeEMs},
	"availability_compare": {scopeEMs},
	"state_timeline":       {scopeEMs},
	"live_tiles":           {scopeEMs},
	// single-EM
	"cycle_distribution": {scopeEM},
	"cycle_drift":        {scopeEM},
	"cycle_spread":       {scopeEM},
	"step_spread":        {scopeEM},
	"cycle_kpis":         {scopeEM},
	// no entity
	"note": {scopeNone},
}

type WidgetScope struct {
	Kind    string   `json:"kind"`
	Line    string   `json:"line,omitempty"`
	Station string   `json:"station,omitempty"`
	EM      string   `json:"em,omitempty"`
	EMs     []string `json:"ems,omitempty"` // "STATION/label"
}

type Widget struct {
	ID    string         `json:"id"`
	Type  string         `json:"type"`
	Span  int            `json:"span"` // columns, 1..4
	Title string         `json:"title,omitempty"`
	Scope *WidgetScope   `json:"scope,omitempty"` // nil = inherit the dashboard scope
	Opts  map[string]any `json:"opts,omitempty"`
}

type DashboardSpec struct {
	Version int          `json:"version"`
	Scope   *WidgetScope `json:"scope,omitempty"` // default for widgets that omit one
	Widgets []Widget     `json:"widgets"`
}

type DashboardMeta struct {
	Slug      string    `json:"slug"`
	Name      string    `json:"name"`
	Author    string    `json:"author"`
	UpdatedAt time.Time `json:"updated_at"`
}

// must start AND end alphanumeric, so "trailing-" is rejected
var slugRe = regexp.MustCompile(`^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$`)

// validateSpec checks structure and that every referenced entity exists, so a
// dashboard cannot be saved pointing at an EM that was never there. (An EM
// deleted AFTER the save is handled at render time instead — validation here
// cannot prevent that.)
func (s *Server) validateSpec(spec *DashboardSpec) error {
	if spec == nil {
		return fmt.Errorf("spec required")
	}
	if spec.Version != 1 {
		return fmt.Errorf("unsupported spec version %d", spec.Version)
	}
	if len(spec.Widgets) > 60 {
		return fmt.Errorf("%d widgets is too many (max 60)", len(spec.Widgets))
	}
	if spec.Scope != nil {
		if err := s.validateScope(spec.Scope); err != nil {
			return fmt.Errorf("dashboard scope: %w", err)
		}
	}
	seen := map[string]bool{}
	for i := range spec.Widgets {
		w := &spec.Widgets[i]
		if w.ID == "" {
			return fmt.Errorf("widget %d has no id", i)
		}
		if seen[w.ID] {
			return fmt.Errorf("duplicate widget id %q", w.ID)
		}
		seen[w.ID] = true

		kinds, ok := widgetTypes[w.Type]
		if !ok {
			return fmt.Errorf("widget %q: unknown type %q", w.ID, w.Type)
		}
		if w.Span < 1 || w.Span > 4 {
			return fmt.Errorf("widget %q: span %d out of range (1-4)", w.ID, w.Span)
		}
		// A widget that needs no entity (a note) must NOT pick up the
		// dashboard's default scope — it would then be validated against a
		// kind it does not accept.
		sc := w.Scope
		if sc == nil && contains(kinds, scopeNone) {
			continue
		}
		if sc == nil {
			sc = spec.Scope
		}
		if sc == nil {
			return fmt.Errorf("widget %q: needs a scope and the dashboard has no default", w.ID)
		}
		if !contains(kinds, sc.Kind) {
			return fmt.Errorf("widget %q (%s): cannot be scoped to %q, expected %s",
				w.ID, w.Type, sc.Kind, strings.Join(kinds, " or "))
		}
		if err := s.validateScope(sc); err != nil {
			return fmt.Errorf("widget %q: %w", w.ID, err)
		}
	}
	return nil
}

func (s *Server) validateScope(sc *WidgetScope) error {
	switch sc.Kind {
	case scopeNone:
		return nil
	case scopeLine:
		if s.findLine(sc.Line) == nil {
			return fmt.Errorf("unknown line %q", sc.Line)
		}
	case scopeStation:
		if s.findStation(sc.Line, sc.Station) == nil {
			return fmt.Errorf("unknown station %q on line %q", sc.Station, sc.Line)
		}
	case scopeEM:
		if _, em := s.findEM(sc.Line, sc.Station, sc.EM); em == nil {
			return fmt.Errorf("unknown EM %s/%s/%s", sc.Line, sc.Station, sc.EM)
		}
	case scopeEMs:
		if len(sc.EMs) == 0 {
			return fmt.Errorf("no EMs selected")
		}
		if len(sc.EMs) > 40 {
			return fmt.Errorf("%d EMs is too many (max 40)", len(sc.EMs))
		}
		for _, ref := range sc.EMs {
			station, label, ok := splitEMRef(ref)
			if !ok {
				return fmt.Errorf("bad EM reference %q, want STATION/label", ref)
			}
			if _, em := s.findEM(sc.Line, station, label); em == nil {
				return fmt.Errorf("unknown EM %s/%s", sc.Line, ref)
			}
		}
	default:
		return fmt.Errorf("unknown scope kind %q", sc.Kind)
	}
	return nil
}

// splitEMRef parses "STATION/label" as used in a scope's EM list.
func splitEMRef(ref string) (station, label string, ok bool) {
	station, label, ok = strings.Cut(ref, "/")
	if !ok || station == "" || label == "" {
		return "", "", false
	}
	return station, label, true
}

func contains(xs []string, v string) bool {
	for _, x := range xs {
		if x == v {
			return true
		}
	}
	return false
}

// findStation resolves a station within a line, case-insensitively.
func (s *Server) findStation(line, station string) *StationInfo {
	l := s.findLine(line)
	if l == nil {
		return nil
	}
	for i := range l.Stations {
		if strings.EqualFold(l.Stations[i].Name, station) {
			return &l.Stations[i]
		}
	}
	return nil
}

// ── handlers ─────────────────────────────────────────────────────────────

func (s *Server) handleListDashboards(w http.ResponseWriter, r *http.Request) {
	rows, err := s.pool.Query(r.Context(), `
	    SELECT slug, name, author, updated_at FROM dashboard ORDER BY name`)
	if err != nil {
		httpErr(w, 500, err)
		return
	}
	defer rows.Close()
	out := []DashboardMeta{}
	for rows.Next() {
		var m DashboardMeta
		if err := rows.Scan(&m.Slug, &m.Name, &m.Author, &m.UpdatedAt); err != nil {
			httpErr(w, 500, err)
			return
		}
		out = append(out, m)
	}
	if err := rows.Err(); err != nil {
		httpErr(w, 500, err)
		return
	}
	writeJSON(w, out)
}

func (s *Server) handleGetDashboard(w http.ResponseWriter, r *http.Request) {
	slug := strings.ToLower(r.PathValue("slug"))
	var m DashboardMeta
	var raw []byte
	err := s.pool.QueryRow(r.Context(), `
	    SELECT slug, name, author, spec, updated_at FROM dashboard WHERE slug=$1`,
		slug).Scan(&m.Slug, &m.Name, &m.Author, &raw, &m.UpdatedAt)
	if err != nil {
		httpErr(w, 404, jsonErr("unknown dashboard"))
		return
	}
	var spec DashboardSpec
	if err := json.Unmarshal(raw, &spec); err != nil {
		httpErr(w, 500, err)
		return
	}
	writeJSON(w, map[string]any{
		"slug": m.Slug, "name": m.Name, "author": m.Author,
		"updated_at": m.UpdatedAt, "spec": spec,
	})
}

// handleSaveDashboard creates or replaces a dashboard. Upsert on slug so the
// editor does not need to know whether it is creating or updating.
func (s *Server) handleSaveDashboard(w http.ResponseWriter, r *http.Request) {
	slug := strings.ToLower(r.PathValue("slug"))
	if !slugRe.MatchString(slug) {
		httpErr(w, 400, jsonErr("slug must be lower-case letters, digits and dashes"))
		return
	}
	var body struct {
		Name   string         `json:"name"`
		Author string         `json:"author"`
		Spec   *DashboardSpec `json:"spec"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		httpErr(w, 400, err)
		return
	}
	body.Name = strings.TrimSpace(body.Name)
	if body.Name == "" {
		httpErr(w, 400, jsonErr("name required"))
		return
	}
	if err := s.validateSpec(body.Spec); err != nil {
		httpErr(w, 400, err)
		return
	}
	raw, err := json.Marshal(body.Spec)
	if err != nil {
		httpErr(w, 500, err)
		return
	}
	if _, err := s.pool.Exec(r.Context(), `
	    INSERT INTO dashboard (slug, name, author, spec, updated_at)
	    VALUES ($1,$2,$3,$4,now())
	    ON CONFLICT (slug) DO UPDATE SET
	      name=EXCLUDED.name, author=EXCLUDED.author,
	      spec=EXCLUDED.spec, updated_at=now()`,
		slug, body.Name, strings.TrimSpace(body.Author), raw); err != nil {
		httpErr(w, 500, err)
		return
	}
	writeJSON(w, map[string]any{"ok": true, "slug": slug})
}

func (s *Server) handleDeleteDashboard(w http.ResponseWriter, r *http.Request) {
	slug := strings.ToLower(r.PathValue("slug"))
	tag, err := s.pool.Exec(r.Context(), `DELETE FROM dashboard WHERE slug=$1`, slug)
	if err != nil {
		httpErr(w, 500, err)
		return
	}
	if tag.RowsAffected() == 0 {
		httpErr(w, 404, jsonErr("unknown dashboard"))
		return
	}
	writeJSON(w, map[string]any{"ok": true})
}

// emIDsForScope resolves an "ems" scope to tracker EM ids, preserving the
// order the user chose so comparison rows stay stable.
func (s *Server) emIDsForScope(_ context.Context, line string, refs []string) ([]int, []string, error) {
	ids := make([]int, 0, len(refs))
	labels := make([]string, 0, len(refs))
	for _, ref := range refs {
		station, label, ok := splitEMRef(ref)
		if !ok {
			return nil, nil, fmt.Errorf("bad EM reference %q", ref)
		}
		_, em := s.findEM(line, station, label)
		if em == nil {
			continue // deleted since the dashboard was saved — skip, do not fail
		}
		ids = append(ids, em.ID)
		labels = append(labels, ref)
	}
	return ids, labels, nil
}
