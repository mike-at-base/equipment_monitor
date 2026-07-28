package compose

import (
	"encoding/json"
	"testing"
)

func leaf(em string) Node          { return Node{EM: em} }
func all(ch ...Node) Node          { return Node{K: K{All: true}, Children: ch} }
func atLeast(k int, ch ...Node) Node { return Node{K: K{N: k}, Children: ch} }

// The motivating case: one mag down for the ENTIRE window (0% availability)
// must not hurt the station while a redundant mag covers it.
func TestRedundantChildFullyDown(t *testing.T) {
	root := atLeast(1, leaf("MAG1"), leaf("MAG2"), leaf("MAG3"), leaf("MAG4"))
	ups := map[string][]Span{
		// MAG1 has no up spans at all
		"MAG2": {{0, 1000}},
		"MAG3": {{0, 1000}},
		"MAG4": {{0, 1000}},
	}
	res := Eval(&root, ups, 0, 1000)
	if got := res.UpMs(nil); got != 1000 {
		t.Fatalf("composed up %d, want 1000 (full window)", got)
	}
	if len(res.Down) != 0 {
		t.Fatalf("unexpected down segments: %+v", res.Down)
	}
}

// Redundancy fails exactly where the outages OVERLAP — the reason you can't
// multiply availabilities.
func TestOverlappingOutages(t *testing.T) {
	root := atLeast(1, leaf("A"), leaf("B"))
	ups := map[string][]Span{
		"A": {{60, 1000}},          // A down [0,60)
		"B": {{0, 40}, {100, 1000}}, // B down [40,100)
	}
	res := Eval(&root, ups, 0, 1000)
	if got := res.UpMs(nil); got != 980 {
		t.Fatalf("composed up %d, want 980", got)
	}
	if len(res.Down) != 1 {
		t.Fatalf("down segments %d, want 1: %+v", len(res.Down), res.Down)
	}
	d := res.Down[0]
	if d.Start != 40 || d.End != 60 {
		t.Fatalf("down [%d,%d), want [40,60)", d.Start, d.End)
	}
	if len(d.Causes) != 2 {
		t.Fatalf("causes %v, want both A and B", d.Causes)
	}
}

func TestSeriesAll(t *testing.T) {
	root := all(leaf("main"), leaf("robot"))
	ups := map[string][]Span{
		"main":  {{0, 1000}},
		"robot": {{0, 300}, {500, 1000}},
	}
	res := Eval(&root, ups, 0, 1000)
	if got := res.UpMs(nil); got != 800 {
		t.Fatalf("composed up %d, want 800", got)
	}
	if len(res.Down) != 1 || res.Down[0].Start != 300 || res.Down[0].End != 500 {
		t.Fatalf("down %+v, want [300,500)", res.Down)
	}
	if len(res.Down[0].Causes) != 1 || res.Down[0].Causes[0] != "robot" {
		t.Fatalf("causes %v, want [robot]", res.Down[0].Causes)
	}
}

func TestKofN(t *testing.T) {
	root := atLeast(2, leaf("A"), leaf("B"), leaf("C"))
	ups := map[string][]Span{
		"A": {{0, 1000}},
		"B": {{0, 400}},  // down from 400
		"C": {{0, 700}},  // down from 700
	}
	// 2-of-3: up while >=2 up -> [0,700); down [700,1000) (only A up)
	res := Eval(&root, ups, 0, 1000)
	if got := res.UpMs(nil); got != 700 {
		t.Fatalf("composed up %d, want 700", got)
	}
	d := res.Down[0]
	if d.Start != 700 || len(d.Causes) != 2 {
		t.Fatalf("down %+v, want [700,1000) caused by B and C", d)
	}
}

// The full ST34000 shape: main AND (rob1 AND any-of-4-mags) AND (rob2 AND
// any-of-4-mags).
func TestNestedStation(t *testing.T) {
	rob1 := all(leaf("ROB1"), atLeast(1, leaf("M1"), leaf("M2"), leaf("M3"), leaf("M4")))
	rob2 := all(leaf("ROB2"), atLeast(1, leaf("M5"), leaf("M6"), leaf("M7"), leaf("M8")))
	root := all(leaf("main"), rob1, rob2)

	fullUp := []Span{{0, 1000}}
	ups := map[string][]Span{
		"main": fullUp, "ROB1": fullUp, "ROB2": fullUp,
		// M1 dead all window; M2/M3/M4 cover except all four overlap-down [200,250)
		"M2": {{0, 200}, {250, 1000}},
		"M3": {{0, 200}, {250, 1000}},
		"M4": {{0, 200}, {250, 1000}},
		// rob2's mags fine
		"M5": fullUp, "M6": fullUp, "M7": fullUp, "M8": fullUp,
	}
	res := Eval(&root, ups, 0, 1000)
	if got := res.UpMs(nil); got != 950 {
		t.Fatalf("composed up %d, want 950", got)
	}
	if len(res.Down) != 1 {
		t.Fatalf("down %+v, want one segment", res.Down)
	}
	d := res.Down[0]
	if d.Start != 200 || d.End != 250 {
		t.Fatalf("down [%d,%d), want [200,250)", d.Start, d.End)
	}
	// all four mags of rob1 are concurrent causes
	if len(d.Causes) != 4 {
		t.Fatalf("causes %v, want the 4 rob1 mags", d.Causes)
	}
	pareto := res.CausePareto(nil)
	if pareto["M1"] != 50 || pareto["M2"] != 50 {
		t.Fatalf("pareto %v, want 50ms charged to each mag", pareto)
	}
	if _, ok := pareto["M5"]; ok {
		t.Fatalf("pareto charged rob2 mags: %v", pareto)
	}
}

// A leaf with no up spans at all (never reported) is down for the window.
func TestMissingLeafIsDown(t *testing.T) {
	root := all(leaf("main"), leaf("ghost"))
	ups := map[string][]Span{"main": {{0, 1000}}}
	res := Eval(&root, ups, 0, 1000)
	if got := res.UpMs(nil); got != 0 {
		t.Fatalf("composed up %d, want 0", got)
	}
	if len(res.Down) != 1 || res.Down[0].Causes[0] != "ghost" {
		t.Fatalf("down %+v, want ghost as cause", res.Down)
	}
}

func TestProductionClip(t *testing.T) {
	root := all(leaf("A"))
	ups := map[string][]Span{"A": {{0, 500}}}
	res := Eval(&root, ups, 0, 1000)
	// production only [400,800): up overlap = [400,500) = 100
	prod := []Span{{400, 800}}
	if got := res.UpMs(prod); got != 100 {
		t.Fatalf("clipped up %d, want 100", got)
	}
	if got := res.CausePareto(prod)["A"]; got != 300 {
		t.Fatalf("clipped cause %d, want 300", got)
	}
}

func TestKJSONRoundTrip(t *testing.T) {
	src := all(leaf("main"), atLeast(2, leaf("A"), leaf("B"), leaf("C")))
	b, err := json.Marshal(src)
	if err != nil {
		t.Fatal(err)
	}
	var back Node
	if err := json.Unmarshal(b, &back); err != nil {
		t.Fatal(err)
	}
	if !back.K.All || back.Children[1].K.N != 2 {
		t.Fatalf("round trip lost thresholds: %s", b)
	}
	if err := back.Validate(map[string]bool{"main": true, "A": true, "B": true, "C": true}); err != nil {
		t.Fatal(err)
	}
}

func TestValidateRejects(t *testing.T) {
	bad := atLeast(5, leaf("A"), leaf("B"))
	if err := bad.Validate(nil); err == nil {
		t.Fatal("k>n accepted")
	}
	empty := Node{K: K{All: true}}
	if err := empty.Validate(nil); err == nil {
		t.Fatal("empty group accepted")
	}
	dup := all(leaf("A"), leaf("A"))
	if err := dup.Validate(nil); err == nil {
		t.Fatal("duplicate member accepted")
	}
	unknown := all(leaf("A"))
	if err := unknown.Validate(map[string]bool{"B": true}); err == nil {
		t.Fatal("unknown member accepted")
	}
}
