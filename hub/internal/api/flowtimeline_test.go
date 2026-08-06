package api

import (
	"testing"
	"time"
)

func mustTime(t *testing.T, s string) time.Time {
	t.Helper()
	v, err := time.Parse(time.RFC3339, s)
	if err != nil {
		t.Fatal(err)
	}
	return v.UTC()
}

// The current partial bucket must be generated. Excluding it made a flow wait
// vanish from the chart the instant it ended: while open it is folded in from
// the live snapshot, but once it closes into state_interval there is no
// bucket for it to join until the clock rolls over.
func TestBucketRangeIncludesCurrentPartialBucket(t *testing.T) {
	from := mustTime(t, "2026-08-06T05:00:00Z") // local midnight
	to := mustTime(t, "2026-08-06T16:20:00Z")   // now, mid-bucket
	start, last := bucketRange(from, to, time.Hour)

	if !start.Equal(mustTime(t, "2026-08-06T05:00:00Z")) {
		t.Fatalf("start %v", start)
	}
	if !last.Equal(mustTime(t, "2026-08-06T16:00:00Z")) {
		t.Fatalf("last %v, want 16:00 (the bucket containing `to`)", last)
	}
	// a wait that ended at 16:15 has to fall inside [last, last+bucket)
	ended := mustTime(t, "2026-08-06T16:15:00Z")
	if ended.Before(last) || !ended.Before(last.Add(time.Hour)) {
		t.Fatalf("16:15 not covered by the last bucket %v", last)
	}
}

func TestBucketRangeAlignsStartToBucket(t *testing.T) {
	from := mustTime(t, "2026-08-06T05:37:00Z")
	to := mustTime(t, "2026-08-06T09:05:00Z")
	start, last := bucketRange(from, to, time.Hour)
	if !start.Equal(mustTime(t, "2026-08-06T05:00:00Z")) {
		t.Fatalf("start %v, want 05:00", start)
	}
	if !last.Equal(mustTime(t, "2026-08-06T09:00:00Z")) {
		t.Fatalf("last %v, want 09:00", last)
	}
}

// A window shorter than one bucket still emits exactly one bar.
func TestBucketRangeSubBucketWindow(t *testing.T) {
	from := mustTime(t, "2026-08-06T11:50:00Z")
	to := mustTime(t, "2026-08-06T11:55:00Z")
	start, last := bucketRange(from, to, time.Hour)
	if !start.Equal(last) {
		t.Fatalf("sub-bucket window should emit one bucket, got %v..%v", start, last)
	}
}

// A short window straddling a boundary emits both buckets.
func TestBucketRangeStraddlesBoundary(t *testing.T) {
	from := mustTime(t, "2026-08-06T11:50:00Z")
	to := mustTime(t, "2026-08-06T12:10:00Z")
	start, last := bucketRange(from, to, time.Hour)
	if !start.Equal(mustTime(t, "2026-08-06T11:00:00Z")) ||
		!last.Equal(mustTime(t, "2026-08-06T12:00:00Z")) {
		t.Fatalf("got %v..%v, want 11:00..12:00", start, last)
	}
}

func TestBucketRangeFifteenMinute(t *testing.T) {
	from := mustTime(t, "2026-08-06T05:00:00Z")
	to := mustTime(t, "2026-08-06T16:07:00Z")
	_, last := bucketRange(from, to, 15*time.Minute)
	if !last.Equal(mustTime(t, "2026-08-06T16:00:00Z")) {
		t.Fatalf("last %v, want 16:00", last)
	}
}
