package api

import (
	"strings"
	"testing"
)

// a Server with a known hierarchy, so scope validation has something to
// resolve against without a database.
func specServer() *Server {
	return &Server{lines: []LineInfo{{
		Name: "CELL1",
		Stations: []StationInfo{
			{Name: "ST34000", EMs: []EMInfo{
				{ID: 1, Station: "ST34000", Label: "main"},
				{ID: 2, Station: "ST34000", Label: "rb01"},
			}},
			{Name: "ST10000", EMs: []EMInfo{
				{ID: 3, Station: "ST10000", Label: "main"},
			}},
		},
	}}}
}

// EM references are fully qualified, so a comparison can span lines.
func emScope(refs ...string) *WidgetScope {
	return &WidgetScope{Kind: scopeEMs, EMs: refs}
}

func TestSpecValidAcceptsRealEntities(t *testing.T) {
	s := specServer()
	spec := &DashboardSpec{
		Version: 1,
		Scope:   &WidgetScope{Kind: scopeLine, Line: "CELL1"},
		Widgets: []Widget{
			{ID: "w1", Type: "cycle_compare", Span: 2,
				Scope: emScope("CELL1/ST34000/main", "CELL1/ST34000/rb01")},
			{ID: "w2", Type: "cycle_drift", Span: 2,
				Scope: &WidgetScope{Kind: scopeEM, Line: "CELL1", Station: "ST34000", EM: "main"}},
			{ID: "w3", Type: "note", Span: 4},
		},
	}
	if err := s.validateSpec(spec); err != nil {
		t.Fatalf("valid spec rejected: %v", err)
	}
}

// The whole point of server-side validation: a dashboard must not be saved
// pointing at equipment that does not exist.
func TestSpecRejectsUnknownEntities(t *testing.T) {
	s := specServer()
	cases := map[string]*WidgetScope{
		"unknown line":    {Kind: scopeLine, Line: "NOPE"},
		"unknown station": {Kind: scopeStation, Line: "CELL1", Station: "ST99999"},
		"unknown em":      {Kind: scopeEM, Line: "CELL1", Station: "ST34000", EM: "ghost"},
	}
	for name, sc := range cases {
		spec := &DashboardSpec{Version: 1, Widgets: []Widget{
			{ID: "w1", Type: "cycle_drift", Span: 1, Scope: sc}}}
		if err := s.validateSpec(spec); err == nil {
			t.Fatalf("%s accepted", name)
		}
	}
	// and inside an EM list
	spec := &DashboardSpec{Version: 1, Widgets: []Widget{
		{ID: "w1", Type: "cycle_compare", Span: 1,
			Scope: emScope("CELL1/ST34000/main", "CELL1/ST34000/ghost")}}}
	err := s.validateSpec(spec)
	if err == nil || !strings.Contains(err.Error(), "ghost") {
		t.Fatalf("unknown EM in list: %v", err)
	}
}

func TestSpecRejectsWrongScopeKindForType(t *testing.T) {
	s := specServer()
	// cycle_drift is single-EM; pointing it at a set must fail
	spec := &DashboardSpec{Version: 1, Widgets: []Widget{
		{ID: "w1", Type: "cycle_drift", Span: 1, Scope: emScope("CELL1/ST34000/main")}}}
	err := s.validateSpec(spec)
	if err == nil || !strings.Contains(err.Error(), "cannot be scoped") {
		t.Fatalf("wrong scope kind accepted: %v", err)
	}
}

func TestSpecRejectsStructuralProblems(t *testing.T) {
	s := specServer()
	em := &WidgetScope{Kind: scopeEM, Line: "CELL1", Station: "ST34000", EM: "main"}
	cases := map[string]*DashboardSpec{
		"bad version": {Version: 2, Widgets: []Widget{
			{ID: "w1", Type: "cycle_drift", Span: 1, Scope: em}}},
		"unknown type": {Version: 1, Widgets: []Widget{
			{ID: "w1", Type: "not_a_widget", Span: 1, Scope: em}}},
		"span 0": {Version: 1, Widgets: []Widget{
			{ID: "w1", Type: "cycle_drift", Span: 0, Scope: em}}},
		"span 5": {Version: 1, Widgets: []Widget{
			{ID: "w1", Type: "cycle_drift", Span: 5, Scope: em}}},
		"missing id": {Version: 1, Widgets: []Widget{
			{Type: "cycle_drift", Span: 1, Scope: em}}},
		"duplicate id": {Version: 1, Widgets: []Widget{
			{ID: "w1", Type: "cycle_drift", Span: 1, Scope: em},
			{ID: "w1", Type: "cycle_kpis", Span: 1, Scope: em}}},
		"empty em list": {Version: 1, Widgets: []Widget{
			{ID: "w1", Type: "cycle_compare", Span: 1, Scope: emScope()}}},
		"bad em ref": {Version: 1, Widgets: []Widget{
			{ID: "w1", Type: "cycle_compare", Span: 1, Scope: emScope("no-slash")}}},
	}
	for name, spec := range cases {
		if err := s.validateSpec(spec); err == nil {
			t.Fatalf("%s accepted", name)
		}
	}
}

// A widget with no scope of its own inherits the dashboard's.
func TestSpecWidgetInheritsDashboardScope(t *testing.T) {
	s := specServer()
	spec := &DashboardSpec{
		Version: 1,
		Scope:   &WidgetScope{Kind: scopeEM, Line: "CELL1", Station: "ST34000", EM: "main"},
		Widgets: []Widget{{ID: "w1", Type: "cycle_kpis", Span: 1}},
	}
	if err := s.validateSpec(spec); err != nil {
		t.Fatalf("inherited scope rejected: %v", err)
	}
	// ...but a scope-needing widget with no default anywhere must fail
	spec.Scope = nil
	if err := s.validateSpec(spec); err == nil {
		t.Fatal("widget with no scope and no default accepted")
	}
	// a "none" widget is fine without any scope
	spec.Widgets = []Widget{{ID: "w1", Type: "note", Span: 1}}
	if err := s.validateSpec(spec); err != nil {
		t.Fatalf("note without scope rejected: %v", err)
	}
}

func TestSlugPattern(t *testing.T) {
	ok := []string{"a", "cycle-time", "cell1-cycles", "x9"}
	bad := []string{"", "-lead", "Upper", "has space", "under_score", "trailing-",
		strings.Repeat("a", 65)}
	for _, v := range ok {
		if !slugRe.MatchString(v) {
			t.Fatalf("slug %q rejected", v)
		}
	}
	for _, v := range bad {
		if slugRe.MatchString(v) {
			t.Fatalf("slug %q accepted", v)
		}
	}
}

// A comparison must be able to span lines — that is the whole reason the
// reference carries one.
func TestSpecAcceptsCrossLineEMList(t *testing.T) {
	s := specServer()
	s.lines = append(s.lines, LineInfo{Name: "CELL2", Stations: []StationInfo{
		{Name: "ST34000", EMs: []EMInfo{{ID: 9, Station: "ST34000", Label: "main"}}}}})
	spec := &DashboardSpec{Version: 1, Widgets: []Widget{
		{ID: "w1", Type: "cycle_compare", Span: 4,
			Scope: emScope("CELL1/ST34000/main", "CELL2/ST34000/main")}}}
	if err := s.validateSpec(spec); err != nil {
		t.Fatalf("cross-line comparison rejected: %v", err)
	}
	// the same EM twice would draw two identical rows
	spec.Widgets[0].Scope = emScope("CELL1/ST34000/main", "CELL1/ST34000/main")
	if err := s.validateSpec(spec); err == nil {
		t.Fatal("duplicate EM accepted")
	}
	// a two-part ref is no longer meaningful: which line?
	spec.Widgets[0].Scope = emScope("ST34000/main")
	if err := s.validateSpec(spec); err == nil {
		t.Fatal("unqualified reference accepted")
	}
}

// The four comparison widgets are the reason custom dashboards exist; a typo
// in widgetTypes would make them unsavable, and only the UI would notice.
func TestCompareWidgetsAcceptEMLists(t *testing.T) {
	s := specServer()
	for _, typ := range []string{"cycle_compare", "availability_compare",
		"state_timeline", "live_tiles"} {
		spec := &DashboardSpec{Version: 1, Widgets: []Widget{
			{ID: "w1", Type: typ, Span: 2, Scope: emScope("CELL1/ST34000/main", "CELL1/ST10000/main")}}}
		if err := s.validateSpec(spec); err != nil {
			t.Errorf("%s rejected a valid EM list: %v", typ, err)
		}
		// and must refuse a single-EM scope, so the UI cannot render it wrong
		spec.Widgets[0].Scope = &WidgetScope{
			Kind: scopeEM, Line: "CELL1", Station: "ST34000", EM: "main"}
		if err := s.validateSpec(spec); err == nil {
			t.Errorf("%s accepted a single-EM scope", typ)
		}
	}
}
