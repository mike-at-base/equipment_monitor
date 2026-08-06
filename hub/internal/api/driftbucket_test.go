package api

import (
	"testing"
	"time"
)

func win(h float64) (time.Time, time.Time) {
	from := time.Date(2026, 8, 6, 6, 0, 0, 0, time.UTC)
	return from, from.Add(time.Duration(h * float64(time.Hour)))
}

// A busy step over a production day should bucket far finer than the 1h the
// old bar-chart sizer gave, so the crosshair reads as continuous.
func TestDriftBucketFineOnBusyDay(t *testing.T) {
	from, to := win(16) // 16 h shift
	name, d := autoStepDriftBucket(from, to, 20000)
	if d > 10*time.Minute {
		t.Fatalf("busy 16h day bucketed at %s, want <= 10m", name)
	}
	if n := int(to.Sub(from) / d); n < 60 {
		t.Fatalf("only %d buckets across the day (%s) — crosshair would feel steppy", n, name)
	}
}

// The sample floor must win when executions are sparse: percentiles over a
// handful of runs are noise, so the bucket has to widen.
func TestDriftBucketRespectsSampleFloor(t *testing.T) {
	from, to := win(16)
	_, dense := autoStepDriftBucket(from, to, 20000)
	name, sparse := autoStepDriftBucket(from, to, 40) // 40 runs all day
	if sparse <= dense {
		t.Fatalf("sparse data bucketed at %s (%v), expected coarser than dense (%v)",
			name, sparse, dense)
	}
	if got := int(to.Sub(from) / sparse); got > 40/minSamplesPerBucket+1 {
		t.Fatalf("%d buckets for 40 executions — under %d samples each",
			got, minSamplesPerBucket)
	}
}

// Very few executions must not produce a per-second chart.
func TestDriftBucketTinySample(t *testing.T) {
	from, to := win(8)
	_, d := autoStepDriftBucket(from, to, 3)
	if n := int(to.Sub(from) / d); n > 2 {
		t.Fatalf("3 executions spread over %d buckets", n)
	}
}

// Short windows should still get a sensible fine bucket, not collapse to 1h.
func TestDriftBucketShortWindow(t *testing.T) {
	from, to := win(1)
	name, d := autoStepDriftBucket(from, to, 5000)
	if d > time.Minute {
		t.Fatalf("1h window bucketed at %s, want <= 1m", name)
	}
}

// Every value the sizer can emit must be parseable back, or the UI's
// bucket override would silently fall back to 1h.
func TestDriftBucketNamesRoundTrip(t *testing.T) {
	for _, b := range niceBuckets {
		got, d := parseBucket(b.name)
		if got != b.name || d != b.d {
			t.Fatalf("parseBucket(%q) = %q/%v, want %q/%v", b.name, got, d, b.name, b.d)
		}
	}
}
