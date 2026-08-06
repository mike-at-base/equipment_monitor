package api

import (
	"testing"
	"time"

	"github.com/mike-at-base/equipment_monitor/hub/internal/compose"
)

func TestComposedDownReasonsUnionsConcurrentEMs(t *testing.T) {
	// Line down for 10 minutes; eight EMs all report the same reason.
	start := time.UnixMilli(0).UTC()
	end := time.UnixMilli(10 * 60 * 1000).UTC()
	downs := []compose.DownSeg{{
		Span:   compose.Span{Start: 0, End: 10 * 60 * 1000},
		Causes: []string{"ST1"},
	}}
	var eps []EpisodeRow
	for i := 0; i < 8; i++ {
		eps = append(eps, EpisodeRow{
			Station: "ST1", EMLabel: "em" + string(rune('a'+i)),
			StartTs: start, EndTs: end,
			ReasonType: "fault", Reason: "air pressure ok",
			Minutes: 10,
		})
	}
	got := composedDownReasons(downs, eps, nil, 5)
	if len(got) != 1 {
		t.Fatalf("reasons %d, want 1: %+v", len(got), got)
	}
	if got[0].Reason != "air pressure ok" {
		t.Fatalf("reason %q", got[0].Reason)
	}
	if got[0].Minutes != 10 {
		t.Fatalf("minutes %.1f, want 10 (union, not 80)", got[0].Minutes)
	}
	if got[0].Count != 1 {
		t.Fatalf("count %d, want 1 composed-down segment", got[0].Count)
	}
}

func TestComposedDownReasonsIgnoresCoveredEMDown(t *testing.T) {
	// No composed-down segments → redundant EM's episode must not appear.
	eps := []EpisodeRow{{
		Station: "ST1", EMLabel: "MAG1",
		StartTs: time.UnixMilli(0).UTC(), EndTs: time.UnixMilli(10 * 60 * 1000).UTC(),
		ReasonType: "fault", Reason: "motor fault", Minutes: 10,
	}}
	got := composedDownReasons(nil, eps, nil, 5)
	if len(got) != 0 {
		t.Fatalf("expected no line reasons while line is up, got %+v", got)
	}
}

func TestComposedDownReasonsSplitsDistinctReasons(t *testing.T) {
	// Same 10 min composed down; two different reasons among causing EMs.
	start := time.UnixMilli(0).UTC()
	end := time.UnixMilli(10 * 60 * 1000).UTC()
	downs := []compose.DownSeg{{Span: compose.Span{Start: 0, End: 10 * 60 * 1000}}}
	eps := []EpisodeRow{
		{StartTs: start, EndTs: end, ReasonType: "fault", Reason: "air pressure ok"},
		{StartTs: start, EndTs: end, ReasonType: "fault", Reason: "estop"},
	}
	got := composedDownReasons(downs, eps, nil, 5)
	if len(got) != 2 {
		t.Fatalf("want 2 reasons, got %+v", got)
	}
	for _, r := range got {
		if r.Minutes != 10 {
			t.Fatalf("%q minutes %.1f, want 10 each", r.Reason, r.Minutes)
		}
	}
}
