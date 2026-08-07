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
	// a list of composable entities: a whole line, or a station within one.
	// Availability for these is the k-of-n composed number, not an average.
	scopeNodes = "nodes"
)

// widgetTypes maps each known widget to the scope kinds it accepts. The
// frontend registry is the source of truth for rendering; this is the
// server-side guard so a malformed spec cannot be persisted.
var widgetTypes = map[string][]string{
	// multi-EM comparisons
	"cycle_compare": {scopeEMs},
	// EMs give each module's own episode availability; stations and lines
	// give the composed k-of-n number, which is the one that describes
	// whether the line could actually run
	"availability_compare": {scopeEMs, scopeNodes},
	"state_timeline":       {scopeEMs},
	"live_tiles":           {scopeEMs},
	// flow loss, to see where the line is waiting rather than broken
	"flow_compare": {scopeNodes},
	"flow_reasons": {scopeNodes},
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
	Kind    string `json:"kind"`
	Line    string `json:"line,omitempty"`
	Station string `json:"station,omitempty"`
	EM      string `json:"em,omitempty"`
	// EMs are fully qualified "LINE/STATION/label". A comparison is not
	// bound to one line — comparing the same machine across two lines is
	// one of the more useful things to put on a dashboard — so the line
	// travels with each reference rather than sitting on the scope.
	EMs []string `json:"ems,omitempty"`
	// Nodes are "LINE" (the whole line) or "LINE/STATION".
	Nodes []string `json:"nodes,omitempty"`
}

type Widget struct {
	ID    string         `json:"id"`
	Type  string         `json:"type"`
	Span  int            `json:"span"` // columns, 1..4
	Title string         `json:"title,omitempty"`
	Scope *WidgetScope   `json:"scope,omitempty"` // omitted only by scope-less types
	Opts  map[string]any `json:"opts,omitempty"`
}

// There is deliberately no dashboard-level default scope. Inheritance meant
// a widget's equipment could be set in two places, and the editor had to
// explain which one won; every widget carrying its own scope is one concept
// instead of two.
type DashboardSpec struct {
	Version int      `json:"version"`
	Widgets []Widget `json:"widgets"`
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
		// a widget that needs no entity (a note) simply has no scope
		if w.Scope == nil {
			if contains(kinds, scopeNone) {
				continue
			}
			return fmt.Errorf("widget %q (%s): needs equipment selected", w.ID, w.Type)
		}
		if !contains(kinds, w.Scope.Kind) {
			return fmt.Errorf("widget %q (%s): cannot be scoped to %q, expected %s",
				w.ID, w.Type, w.Scope.Kind, strings.Join(kinds, " or "))
		}
		if err := s.validateScope(w.Scope); err != nil {
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
		seen := map[string]bool{}
		for _, ref := range sc.EMs {
			line, station, label, ok := splitEMRef(ref)
			if !ok {
				return fmt.Errorf("bad EM reference %q, want LINE/STATION/label", ref)
			}
			if seen[ref] {
				return fmt.Errorf("EM %s listed twice", ref)
			}
			seen[ref] = true
			if _, em := s.findEM(line, station, label); em == nil {
				return fmt.Errorf("unknown EM %s", ref)
			}
		}
	case scopeNodes:
		if len(sc.Nodes) == 0 {
			return fmt.Errorf("no stations or lines selected")
		}
		if len(sc.Nodes) > 40 {
			return fmt.Errorf("%d nodes is too many (max 40)", len(sc.Nodes))
		}
		seen := map[string]bool{}
		for _, ref := range sc.Nodes {
			if seen[ref] {
				return fmt.Errorf("%s listed twice", ref)
			}
			seen[ref] = true
			line, station, ok := splitNodeRef(ref)
			if !ok {
				return fmt.Errorf("bad reference %q, want LINE or LINE/STATION", ref)
			}
			if station == "" {
				if s.findLine(line) == nil {
					return fmt.Errorf("unknown line %q", line)
				}
			} else if s.findStation(line, station) == nil {
				return fmt.Errorf("unknown station %s", ref)
			}
		}
	default:
		return fmt.Errorf("unknown scope kind %q", sc.Kind)
	}
	return nil
}

// splitNodeRef parses "LINE" or "LINE/STATION". An empty station means the
// reference is to the line as a whole.
func splitNodeRef(ref string) (line, station string, ok bool) {
	parts := strings.Split(ref, "/")
	switch len(parts) {
	case 1:
		if parts[0] == "" {
			return "", "", false
		}
		return parts[0], "", true
	case 2:
		if parts[0] == "" || parts[1] == "" {
			return "", "", false
		}
		return parts[0], parts[1], true
	}
	return "", "", false
}

// splitEMRef parses "LINE/STATION/label" as used in a scope's EM list.
func splitEMRef(ref string) (line, station, label string, ok bool) {
	parts := strings.Split(ref, "/")
	if len(parts) != 3 || parts[0] == "" || parts[1] == "" || parts[2] == "" {
		return "", "", "", false
	}
	return parts[0], parts[1], parts[2], true
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
