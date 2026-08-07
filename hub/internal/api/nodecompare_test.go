package api

import (
	"testing"
	"time"
)

func ts(min int) time.Time {
	return time.Date(2026, 8, 1, 0, min, 0, 0, time.UTC)
}

func span(a, b int) [2]time.Time { return [2]time.Time{ts(a), ts(b)} }

// The reason this function exists: several EMs starved at the same moment is
// ONE outage in clock time, however many modules report it.
func TestUnionMsCollapsesConcurrentSpans(t *testing.T) {
	whole := [][2]time.Time{span(0, 600)}
	cases := []struct {
		name  string
		spans [][2]time.Time
		want  int64 // minutes
	}{
		{"empty", nil, 0},
		{"one", [][2]time.Time{span(10, 20)}, 10},
		{"disjoint", [][2]time.Time{span(10, 20), span(30, 45)}, 25},
		// four modules starved over the same ten minutes: still ten
		{"identical", [][2]time.Time{
			span(10, 20), span(10, 20), span(10, 20), span(10, 20)}, 10},
		{"overlapping", [][2]time.Time{span(10, 25), span(20, 40)}, 30},
		{"nested", [][2]time.Time{span(10, 50), span(20, 30)}, 40},
		{"touching", [][2]time.Time{span(10, 20), span(20, 30)}, 20},
		// order must not matter
		{"unsorted", [][2]time.Time{span(30, 45), span(10, 20)}, 25},
	}
	for _, c := range cases {
		got := unionMs(c.spans, whole) / 60000
		if got != c.want {
			t.Errorf("%s: got %d min, want %d", c.name, got, c.want)
		}
	}
}

// Flow loss is production-clipped so it lines up with the availability number
// computed over the same ranges.
func TestUnionMsClipsToProduction(t *testing.T) {
	prod := [][2]time.Time{span(100, 200), span(300, 400)}
	cases := []struct {
		name  string
		spans [][2]time.Time
		want  int64
	}{
		{"entirely outside", [][2]time.Time{span(0, 50)}, 0},
		{"in the break between shifts", [][2]time.Time{span(210, 290)}, 0},
		{"straddling the start", [][2]time.Time{span(50, 150)}, 50},
		{"spanning the break", [][2]time.Time{span(150, 350)}, 100},
		{"merged then clipped", [][2]time.Time{span(150, 250), span(240, 350)}, 100},
	}
	for _, c := range cases {
		got := unionMs(c.spans, prod) / 60000
		if got != c.want {
			t.Errorf("%s: got %d min, want %d", c.name, got, c.want)
		}
	}
}

func TestSplitNodeRef(t *testing.T) {
	cases := []struct {
		ref           string
		line, station string
		ok            bool
	}{
		{"CELL1", "CELL1", "", true},
		{"CELL1/ST34000", "CELL1", "ST34000", true},
		{"", "", "", false},
		{"/ST34000", "", "", false},
		{"CELL1/", "", "", false},
		{"CELL1/ST34000/main", "", "", false}, // that is an EM, not a node
	}
	for _, c := range cases {
		line, station, ok := splitNodeRef(c.ref)
		if ok != c.ok || line != c.line || station != c.station {
			t.Errorf("%q: got (%q,%q,%v), want (%q,%q,%v)",
				c.ref, line, station, ok, c.line, c.station, c.ok)
		}
	}
}

func TestSpecValidatesNodeScope(t *testing.T) {
	s := specServer()
	ok := &DashboardSpec{Version: 1, Widgets: []Widget{
		{ID: "w1", Type: "flow_compare", Span: 2, Scope: &WidgetScope{
			Kind: scopeNodes, Nodes: []string{"CELL1", "CELL1/ST34000"}}}}}
	if err := s.validateSpec(ok); err != nil {
		t.Fatalf("valid node scope rejected: %v", err)
	}
	bad := map[string][]string{
		"unknown line":    {"NOPE"},
		"unknown station": {"CELL1/ST99999"},
		"empty":           {},
		"duplicate":       {"CELL1", "CELL1"},
		"an EM ref":       {"CELL1/ST34000/main"},
	}
	for name, nodes := range bad {
		spec := &DashboardSpec{Version: 1, Widgets: []Widget{
			{ID: "w1", Type: "flow_compare", Span: 2,
				Scope: &WidgetScope{Kind: scopeNodes, Nodes: nodes}}}}
		if err := s.validateSpec(spec); err == nil {
			t.Errorf("%s accepted", name)
		}
	}
	// availability_compare takes either shape; flow_compare does not
	both := &DashboardSpec{Version: 1, Widgets: []Widget{
		{ID: "w1", Type: "availability_compare", Span: 2, Scope: &WidgetScope{
			Kind: scopeNodes, Nodes: []string{"CELL1/ST34000"}}}}}
	if err := s.validateSpec(both); err != nil {
		t.Fatalf("availability_compare rejected a node scope: %v", err)
	}
	both.Widgets[0].Scope = emScope("CELL1/ST34000/main")
	if err := s.validateSpec(both); err != nil {
		t.Fatalf("availability_compare rejected an EM scope: %v", err)
	}
	flow := &DashboardSpec{Version: 1, Widgets: []Widget{
		{ID: "w1", Type: "flow_compare", Span: 2,
			Scope: emScope("CELL1/ST34000/main")}}}
	if err := s.validateSpec(flow); err == nil {
		t.Fatal("flow_compare accepted an EM scope")
	}
}
