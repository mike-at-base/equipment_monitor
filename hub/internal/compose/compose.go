// Package compose evaluates k-of-n availability trees over time.
//
// You cannot multiply child availabilities to get a station's availability —
// redundancy only helps when the outages don't overlap in time. So this
// package works in the time domain: it sweeps every child state-change
// boundary in the window, evaluates the boolean tree on each homogeneous
// segment, and returns the composed up spans plus, for each composed-down
// segment, the leaves that broke the threshold (cause attribution).
package compose

import (
	"encoding/json"
	"fmt"
	"sort"
)

// K is a group node's threshold: "all" (series) or an integer minimum
// ("any" is k=1). Marshals to/from `"all"` or a JSON number.
type K struct {
	All bool
	N   int
}

func (k K) MarshalJSON() ([]byte, error) {
	if k.All {
		return []byte(`"all"`), nil
	}
	return json.Marshal(k.N)
}

func (k *K) UnmarshalJSON(b []byte) error {
	if string(b) == `"all"` {
		k.All, k.N = true, 0
		return nil
	}
	k.All = false
	return json.Unmarshal(b, &k.N)
}

// Node is one vertex of an availability tree. A leaf names an EM (station
// scope) or a station (line scope); a group has K and Children.
type Node struct {
	EM       string `json:"em,omitempty"`
	Station  string `json:"station,omitempty"`
	K        K      `json:"k,omitempty"`
	Children []Node `json:"children,omitempty"`
}

func (n *Node) IsLeaf() bool { return n.EM != "" || n.Station != "" }

// LeafKey returns the identity this leaf resolves against (EM label or
// station name — the scopes never mix inside one tree).
func (n *Node) LeafKey() string {
	if n.EM != "" {
		return n.EM
	}
	return n.Station
}

// Leaves returns every leaf key in the tree, in document order.
func (n *Node) Leaves() []string {
	if n.IsLeaf() {
		return []string{n.LeafKey()}
	}
	var out []string
	for i := range n.Children {
		out = append(out, n.Children[i].Leaves()...)
	}
	return out
}

// Validate checks structural sanity: leaves name something, groups have
// children, thresholds are within 1..len(children), and every leaf key is in
// the allowed set (pass nil to skip membership checking).
func (n *Node) Validate(allowed map[string]bool) error {
	if n.IsLeaf() {
		if n.EM != "" && n.Station != "" {
			return fmt.Errorf("leaf names both em %q and station %q", n.EM, n.Station)
		}
		if len(n.Children) > 0 {
			return fmt.Errorf("leaf %q has children", n.LeafKey())
		}
		if allowed != nil && !allowed[n.LeafKey()] {
			return fmt.Errorf("unknown member %q", n.LeafKey())
		}
		return nil
	}
	if len(n.Children) == 0 {
		return fmt.Errorf("group has no children")
	}
	if !n.K.All && (n.K.N < 1 || n.K.N > len(n.Children)) {
		return fmt.Errorf("threshold %d out of range for %d children", n.K.N, len(n.Children))
	}
	seen := map[string]bool{}
	for i := range n.Children {
		if err := n.Children[i].Validate(allowed); err != nil {
			return err
		}
		if c := &n.Children[i]; c.IsLeaf() {
			if seen[c.LeafKey()] {
				return fmt.Errorf("member %q appears twice in one group", c.LeafKey())
			}
			seen[c.LeafKey()] = true
		}
	}
	return nil
}

// need returns the minimum number of up children for this group to be up.
func (n *Node) need() int {
	if n.K.All {
		return len(n.Children)
	}
	return n.K.N
}

// Span is a half-open [Start,End) interval in epoch milliseconds.
type Span struct {
	Start int64 `json:"start"`
	End   int64 `json:"end"`
}

// DownSeg is a composed-unavailable segment with the leaves that caused it.
// When redundancy collapses (a k-of-n falls below k), every down child of
// that group is a cause — concurrent causes are each charged the full
// segment in the pareto, which is the honest attribution for "what do I fix".
type DownSeg struct {
	Span
	Causes []string `json:"causes"`
}

// Result of evaluating a tree over a window.
type Result struct {
	Up   []Span    // composed available spans, merged/sorted
	Down []DownSeg // composed unavailable segments with causes
}

// Eval sweeps [from,to) and evaluates root on each segment. ups maps each
// leaf key to its sorted, non-overlapping up spans; any instant not covered
// by an up span is down (that's how manual/offline/no-data count against
// composition). Leaves missing from ups are down for the whole window.
func Eval(root *Node, ups map[string][]Span, from, to int64) Result {
	if to <= from {
		return Result{}
	}
	// segment boundaries: window edges + every span edge inside the window
	bounds := []int64{from, to}
	for _, spans := range ups {
		for _, s := range spans {
			if s.Start > from && s.Start < to {
				bounds = append(bounds, s.Start)
			}
			if s.End > from && s.End < to {
				bounds = append(bounds, s.End)
			}
		}
	}
	sort.Slice(bounds, func(i, j int) bool { return bounds[i] < bounds[j] })
	// dedupe
	uniq := bounds[:1]
	for _, b := range bounds[1:] {
		if b != uniq[len(uniq)-1] {
			uniq = append(uniq, b)
		}
	}

	var res Result
	for i := 0; i+1 < len(uniq); i++ {
		segStart, segEnd := uniq[i], uniq[i+1]
		up, causes := evalAt(root, ups, segStart)
		if up {
			// merge with previous up span when contiguous
			if n := len(res.Up); n > 0 && res.Up[n-1].End == segStart {
				res.Up[n-1].End = segEnd
				continue
			}
			res.Up = append(res.Up, Span{segStart, segEnd})
		} else {
			// merge contiguous down segments only when causes match
			if n := len(res.Down); n > 0 && res.Down[n-1].End == segStart &&
				sameCauses(res.Down[n-1].Causes, causes) {
				res.Down[n-1].End = segEnd
				continue
			}
			res.Down = append(res.Down, DownSeg{Span{segStart, segEnd}, causes})
		}
	}
	return res
}

// evalAt evaluates the tree at instant t. For a down result it returns the
// leaves that broke the threshold: every down child of a failing group,
// recursively.
func evalAt(n *Node, ups map[string][]Span, t int64) (bool, []string) {
	if n.IsLeaf() {
		if upAt(ups[n.LeafKey()], t) {
			return true, nil
		}
		return false, []string{n.LeafKey()}
	}
	upCount := 0
	var downCauses []string
	for i := range n.Children {
		up, causes := evalAt(&n.Children[i], ups, t)
		if up {
			upCount++
		} else {
			downCauses = append(downCauses, causes...)
		}
	}
	if upCount >= n.need() {
		return true, nil
	}
	return false, downCauses
}

// upAt reports whether sorted spans cover instant t.
func upAt(spans []Span, t int64) bool {
	i := sort.Search(len(spans), func(i int) bool { return spans[i].End > t })
	return i < len(spans) && spans[i].Start <= t
}

func sameCauses(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// UpMs sums the up spans, optionally clipped to ranges (pass nil for none).
func (r Result) UpMs(ranges []Span) int64 {
	var total int64
	for _, s := range r.Up {
		total += ClipMs(s, ranges)
	}
	return total
}

// DownMs sums composed-down spans, optionally clipped to ranges.
func (r Result) DownMs(ranges []Span) int64 {
	var total int64
	for _, d := range r.Down {
		total += ClipMs(d.Span, ranges)
	}
	return total
}

// CausePareto charges each cause the duration of every down segment it
// appears in, clipped to ranges. Concurrent causes are each charged in full.
func (r Result) CausePareto(ranges []Span) map[string]int64 {
	out := map[string]int64{}
	for _, d := range r.Down {
		ms := ClipMs(d.Span, ranges)
		if ms == 0 {
			continue
		}
		for _, c := range d.Causes {
			out[c] += ms
		}
	}
	return out
}

// ClipMs returns the length of s, optionally intersected with ranges
// (pass nil for the unclipped length).
func ClipMs(s Span, ranges []Span) int64 {
	return clipMs(s, ranges)
}

func clipMs(s Span, ranges []Span) int64 {
	if ranges == nil {
		return s.End - s.Start
	}
	var total int64
	for _, r := range ranges {
		lo, hi := max64(s.Start, r.Start), min64(s.End, r.End)
		if hi > lo {
			total += hi - lo
		}
	}
	return total
}

func max64(a, b int64) int64 {
	if a > b {
		return a
	}
	return b
}

func min64(a, b int64) int64 {
	if a < b {
		return a
	}
	return b
}
