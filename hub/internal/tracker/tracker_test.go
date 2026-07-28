package tracker

// Golden scenario tests — the same sequences that validated the Python
// collector end-to-end against a real database, replayed against the Go
// port: fast steps, scan-latched fault reasons, interlock pauses with all
// failing conditions, reset ack (MTTR split), mode windows, deduced flow
// states, and cycle work/exchange splits.

import (
	"testing"
	"time"

	"github.com/mike-at-base/equipment_monitor/hub/internal/model"
	"github.com/mike-at-base/equipment_monitor/hub/internal/wire"
)

const (
	AUTO    = wire.BitAutomatic
	FAULT   = wire.BitFault
	RUN     = wire.BitRunning
	PAUSED  = wire.BitPaused
	STEPFLT = wire.BitStepFault
	ILKOK   = wire.BitInterlockOk
	RESET   = wire.BitReset
	MDRY    = 0x0008
)

type capture struct {
	intervals []model.StateInterval
	steps     []model.StepEvent
	cycles    []model.Cycle
	modes     []model.ModeInterval
	operator  []model.OperatorEvent
	episodes  []model.DownEpisode
}

func (c *capture) AddDownEpisode(v model.DownEpisode) { c.episodes = append(c.episodes, v) }

func (c *capture) AddStateInterval(v model.StateInterval) { c.intervals = append(c.intervals, v) }
func (c *capture) AddStepEvent(v model.StepEvent)         { c.steps = append(c.steps, v) }
func (c *capture) AddCycle(v model.Cycle)                 { c.cycles = append(c.cycles, v) }
func (c *capture) AddModeInterval(v model.ModeInterval)   { c.modes = append(c.modes, v) }
func (c *capture) AddOperatorEvent(v model.OperatorEvent) { c.operator = append(c.operator, v) }

type sim struct {
	t   *EM
	cap *capture
	now time.Time
	seq uint32
}

func newSim() *sim {
	cap := &capture{}
	cfg := Config{
		EMID: 1, Station: "ST10000", EMLabel: "main",
		Sequences: map[int16]SeqConfig{
			1: {Index: 1, IsProduction: true, CycleStart: "20", CycleComplete: "70"},
		},
	}
	return &sim{t: New(cfg, cap), cap: cap,
		now: time.Date(2026, 7, 22, 12, 0, 0, 0, time.UTC)}
}

func (s *sim) send(bits, modes uint16, step, desc, alarm, ilk, cond, waiting string) {
	s.seq++
	raw := wire.BuildTest(wire.MsgEvent, bits, modes, s.seq, 1,
		step, desc, alarm, ilk, cond, waiting, s.now)
	d, err := wire.Decode(raw)
	if err != nil {
		panic(err)
	}
	s.t.Ingest(d, s.now)
}

func (s *sim) advance(d time.Duration) { s.now = s.now.Add(d) }

func TestFastStepsAllRecorded(t *testing.T) {
	s := newSim()
	for _, step := range []string{"10", "20", "30", "40", "50"} {
		s.send(AUTO|RUN|ILKOK, 0, step, "desc "+step, "", "", "", "")
		s.advance(2 * time.Millisecond) // faster than any sampling interval
	}
	// steps 10..40 closed (50 still open)
	if len(s.cap.steps) != 4 {
		t.Fatalf("step events: got %d, want 4", len(s.cap.steps))
	}
	for i, want := range []string{"10", "20", "30", "40"} {
		if s.cap.steps[i].StepName != want {
			t.Fatalf("step[%d] = %q, want %q", i, s.cap.steps[i].StepName, want)
		}
		if s.cap.steps[i].DurationMs != 2 {
			t.Fatalf("step[%d] duration %d ms", i, s.cap.steps[i].DurationMs)
		}
	}
}

func TestFaultReasonComposition(t *testing.T) {
	s := newSim()
	s.send(AUTO|RUN|ILKOK, 0, "50", "Unclamp", "", "", "", "")
	s.advance(3 * time.Second)
	// timeout fault with scan-latched permissive conditions
	s.send(AUTO|FAULT|STEPFLT|ILKOK, 0, "50", "Unclamp",
		"Unclamp Timeout", "", "Clamp retracted not made; Part release confirm not made", "")
	s.advance(20 * time.Second)
	s.send(AUTO|RUN|ILKOK, 0, "60", "Index", "Unclamp Timeout", "", "", "") // stale alarm text, real PLC behavior
	last := s.cap.intervals[len(s.cap.intervals)-1]
	if last.State != model.StateDown || last.ReasonType != model.ReasonStepFault {
		t.Fatalf("state %q rtype %q", last.State, last.ReasonType)
	}
	want := "Unclamp Timeout — Clamp retracted not made; Part release confirm not made"
	if last.Reason != want {
		t.Fatalf("reason %q", last.Reason)
	}
	if last.EndTs.Sub(last.StartTs) != 20*time.Second {
		t.Fatalf("down duration %v", last.EndTs.Sub(last.StartTs))
	}
	// the closed step 50 carries the fault flag
	for _, st := range s.cap.steps {
		if st.StepName == "50" && !st.WasFaulted {
			t.Fatal("step 50 not marked faulted")
		}
	}
}

func TestExternalFaultUsesAlarmAlone(t *testing.T) {
	s := newSim()
	s.send(AUTO|RUN|ILKOK, 0, "70", "Robot pick", "", "", "", "")
	s.advance(time.Second)
	s.send(AUTO|FAULT|STEPFLT|ILKOK, 0, "70", "Robot pick",
		"Robot fault: servo alarm SRVO-062", "", "", "")
	s.advance(time.Second)
	s.send(AUTO|RUN|ILKOK, 0, "70", "Robot pick", "Robot fault: servo alarm SRVO-062", "", "", "")
	last := s.cap.intervals[len(s.cap.intervals)-1]
	if last.Reason != "Robot fault: servo alarm SRVO-062" {
		t.Fatalf("reason %q", last.Reason)
	}
}

func TestInterlockPauseWithAllConditions(t *testing.T) {
	s := newSim()
	s.send(AUTO|RUN|ILKOK, 0, "60", "Index", "", "", "", "")
	s.advance(time.Second)
	// interlock drops -> PLC pauses; NO fault bit (the v1 blind spot)
	s.send(AUTO|PAUSED, 0, "60", "Index", "", "Safety gate 1 not closed; Air dump active", "", "")
	s.advance(30 * time.Second)
	s.send(AUTO|RUN|ILKOK, 0, "60", "Index", "", "", "", "")
	last := s.cap.intervals[len(s.cap.intervals)-1]
	if last.State != model.StateDown || last.ReasonType != model.ReasonInterlock {
		t.Fatalf("state %q rtype %q", last.State, last.ReasonType)
	}
	if last.Reason != "Safety gate 1 not closed; Air dump active" {
		t.Fatalf("reason %q", last.Reason)
	}
}

func TestOperatorPauseVsInterlock(t *testing.T) {
	s := newSim()
	s.send(AUTO|RUN|ILKOK, 0, "60", "Index", "", "", "", "")
	s.advance(time.Second)
	s.send(AUTO|PAUSED|ILKOK, 0, "60", "Index", "", "", "", "") // healthy interlock
	s.advance(5 * time.Second)
	s.send(AUTO|RUN|ILKOK, 0, "60", "Index", "", "", "", "")
	last := s.cap.intervals[len(s.cap.intervals)-1]
	if last.State != model.StatePaused || last.Reason != "Operator pause" {
		t.Fatalf("state %q reason %q", last.State, last.Reason)
	}
}

func TestResetAcksDownInterval(t *testing.T) {
	s := newSim()
	s.send(AUTO|RUN|ILKOK, 0, "50", "Unclamp", "", "", "", "")
	s.advance(time.Second)
	s.send(AUTO|FAULT|STEPFLT|ILKOK, 0, "50", "Unclamp", "Unclamp Timeout", "", "", "")
	s.advance(45 * time.Second) // response time
	s.send(AUTO|FAULT|STEPFLT|ILKOK|RESET, 0, "50", "Unclamp", "Unclamp Timeout", "", "", "")
	s.advance(15 * time.Second) // repair time
	s.send(AUTO|RUN|ILKOK, 0, "60", "Index", "Unclamp Timeout", "", "", "")

	if len(s.cap.operator) != 1 || s.cap.operator[0].Event != "reset" {
		t.Fatalf("operator events: %+v", s.cap.operator)
	}
	last := s.cap.intervals[len(s.cap.intervals)-1]
	if last.AckTs == nil {
		t.Fatal("down interval not acked")
	}
	if resp := last.AckTs.Sub(last.StartTs); resp != 45*time.Second {
		t.Fatalf("response time %v", resp)
	}
}

func TestModeWindows(t *testing.T) {
	s := newSim()
	s.send(AUTO|RUN|ILKOK, 0, "60", "Index", "", "", "", "")
	s.advance(time.Second)
	s.send(AUTO|RUN|ILKOK, MDRY, "60", "Index", "", "", "", "")
	s.advance(90 * time.Second)
	s.send(AUTO|RUN|ILKOK, 0, "60", "Index", "", "", "", "")
	if len(s.cap.modes) != 1 {
		t.Fatalf("mode intervals: %d", len(s.cap.modes))
	}
	m := s.cap.modes[0]
	if m.Flag != "dry_cycle" || m.EndTs.Sub(m.StartTs) != 90*time.Second {
		t.Fatalf("mode %q dur %v", m.Flag, m.EndTs.Sub(m.StartTs))
	}
}

// starved/blocked come strictly from the configured step lists — nothing is
// inferred from cycle position or reason keywords.
func TestFlowClassificationFromConfig(t *testing.T) {
	cap := &capture{}
	cfg := Config{
		EMID: 1, Station: "ST10000", EMLabel: "main",
		Sequences: map[int16]SeqConfig{
			1: {Index: 1, IsProduction: true, CycleStart: "20", CycleComplete: "70",
				StarvedSteps: map[string]bool{"20": true},
				BlockedSteps: map[string]bool{"70": true}},
		},
	}
	s := &sim{t: New(cfg, cap), cap: cap,
		now: time.Date(2026, 7, 22, 12, 0, 0, 0, time.UTC)}

	// blocked: dwell waiting at a configured blocked step
	s.send(AUTO|RUN|ILKOK, 0, "70", "Release part", "", "", "", "")
	s.advance(11 * time.Second)
	s.send(AUTO|RUN|ILKOK, 0, "70", "Release part", "", "", "", "Downstream pallet stop occupied")
	s.advance(8 * time.Second)
	// waiting set changes while still blocked -> reason updates in place
	s.send(AUTO|RUN|ILKOK, 0, "70", "Release part", "", "", "",
		"Downstream pallet stop occupied; Outfeed conveyor not clear")
	s.advance(4 * time.Second)
	s.send(AUTO|RUN|ILKOK, 0, "70", "Release part", "", "", "", "") // wait clears
	blocked := s.cap.intervals[len(s.cap.intervals)-1]
	if blocked.State != model.StateBlocked {
		t.Fatalf("state %q", blocked.State)
	}
	if blocked.Reason != "Downstream pallet stop occupied; Outfeed conveyor not clear" {
		t.Fatalf("reason %q", blocked.Reason)
	}
	if blocked.EndTs.Sub(blocked.StartTs) != 12*time.Second {
		t.Fatalf("blocked duration %v", blocked.EndTs.Sub(blocked.StartTs))
	}

	// starved: dwell waiting at a configured starved step
	s.advance(time.Second)
	s.send(AUTO|RUN|ILKOK, 0, "20", "Close clamp", "", "", "", "Part present at infeed")
	s.advance(6 * time.Second)
	s.send(AUTO|RUN|ILKOK, 0, "20", "Close clamp", "", "", "", "")
	starved := s.cap.intervals[len(s.cap.intervals)-1]
	if starved.State != model.StateStarved || starved.Reason != "Part present at infeed" {
		t.Fatalf("state %q reason %q", starved.State, starved.Reason)
	}

	// no inference: waiting at an unlisted step is generic wait, even when the
	// reason text contains flow keywords that the old deducer would have caught.
	s.advance(time.Second)
	s.send(AUTO|RUN|ILKOK, 0, "40", "Weld", "", "", "", "Downstream conveyor not clear")
	s.advance(6 * time.Second)
	s.send(AUTO|RUN|ILKOK, 0, "40", "Weld", "", "", "", "")
	wait := s.cap.intervals[len(s.cap.intervals)-1]
	if wait.State != model.StateWait {
		t.Fatalf("unlisted step: state %q, want wait", wait.State)
	}
}

func TestCycleWorkExchangeSplit(t *testing.T) {
	s := newSim()
	s.send(AUTO|RUN|ILKOK, 0, "20", "Close clamp", "", "", "", "") // cycle 1 start
	s.advance(30 * time.Second)
	s.send(AUTO|RUN|ILKOK, 0, "70", "Release part", "", "", "", "") // work ends
	s.advance(10 * time.Second)
	s.send(AUTO|RUN|ILKOK, 0, "20", "Close clamp", "", "", "", "") // cycle 1 ends, 2 starts
	if len(s.cap.cycles) != 1 {
		t.Fatalf("cycles: %d", len(s.cap.cycles))
	}
	c := s.cap.cycles[0]
	if c.TotalMs != 40000 || c.WorkMs == nil || *c.WorkMs != 30000 ||
		c.ExchangeMs == nil || *c.ExchangeMs != 10000 {
		t.Fatalf("cycle %+v", c)
	}
	if c.Interrupted {
		t.Fatal("clean cycle marked interrupted")
	}
}

func TestInterruptedCycle(t *testing.T) {
	s := newSim()
	s.send(AUTO|RUN|ILKOK, 0, "20", "Close clamp", "", "", "", "")
	s.advance(5 * time.Second)
	s.send(AUTO|FAULT|STEPFLT|ILKOK, 0, "30", "Advance press", "Press Timeout", "", "", "")
	s.advance(20 * time.Second)
	s.send(AUTO|RUN|ILKOK, 0, "30", "Advance press", "Press Timeout", "", "", "")
	s.advance(20 * time.Second)
	s.send(AUTO|RUN|ILKOK, 0, "70", "Release part", "Press Timeout", "", "", "")
	s.advance(5 * time.Second)
	s.send(AUTO|RUN|ILKOK, 0, "20", "Close clamp", "Press Timeout", "", "", "")
	if len(s.cap.cycles) != 1 || !s.cap.cycles[0].Interrupted {
		t.Fatalf("cycles: %+v", s.cap.cycles)
	}
}

func TestOfflineInterval(t *testing.T) {
	s := newSim()
	s.send(AUTO|RUN|ILKOK, 0, "30", "Advance press", "", "", "", "")
	s.advance(time.Minute)
	s.t.MarkOffline(s.now)
	if s.t.State() != model.StateOffline {
		t.Fatalf("state %q", s.t.State())
	}
	prod := s.cap.intervals[len(s.cap.intervals)-1]
	if prod.State != model.StateProductive || prod.EndTs.Sub(prod.StartTs) != time.Minute {
		t.Fatalf("closed interval %+v", prod)
	}
	s.advance(time.Minute)
	s.send(AUTO|RUN|ILKOK, 0, "30", "Advance press", "", "", "", "")
	off := s.cap.intervals[len(s.cap.intervals)-1]
	if off.State != model.StateOffline {
		t.Fatalf("expected offline close, got %q", off.State)
	}
}

func TestInterlockBeatsManual(t *testing.T) {
	s := newSim()
	s.send(AUTO|RUN|ILKOK, 0, "30", "Advance press", "", "", "", "")
	s.advance(time.Second)
	// interlock trip that ALSO drops the EM out of automatic — must be
	// down/interlock with the failing conditions, never reasonless manual
	s.send(0, 0, "30", "Advance press", "", "Air pressure OK", "", "")
	s.advance(30 * time.Second)
	s.send(AUTO|RUN|ILKOK, 0, "30", "Advance press", "", "", "", "")
	last := s.cap.intervals[len(s.cap.intervals)-1]
	if last.State != model.StateDown || last.ReasonType != model.ReasonInterlock {
		t.Fatalf("state %q rtype %q", last.State, last.ReasonType)
	}
	if last.Reason != "Air pressure OK" {
		t.Fatalf("reason %q", last.Reason)
	}
}

func TestTrueManualStaysManual(t *testing.T) {
	s := newSim()
	s.send(AUTO|RUN|ILKOK, 0, "30", "Advance press", "", "", "", "")
	s.advance(time.Second)
	// out of automatic with a HEALTHY interlock = genuine manual mode
	s.send(ILKOK, 0, "30", "Advance press", "", "", "", "")
	s.advance(10 * time.Second)
	s.send(AUTO|RUN|ILKOK, 0, "30", "Advance press", "", "", "", "")
	last := s.cap.intervals[len(s.cap.intervals)-1]
	if last.State != model.StateManual {
		t.Fatalf("state %q", last.State)
	}
}

// The full latch-fault story: fault, gate opened to fix it, reset (ack),
// retry runs the faulted step and re-faults, then finally recovers.
// One episode, root = the original sequence fault, retry time not counted
// as recovery, raw intervals still record every phase.
func TestDownEpisodeLatchesRootCause(t *testing.T) {
	s := newSim()
	s.send(AUTO|RUN|ILKOK, 0, "50", "Unclamp", "", "", "", "")
	s.advance(time.Second)
	// 1) sequence fault (episode opens, root locked)
	s.send(AUTO|FAULT|STEPFLT|ILKOK, 0, "50", "Unclamp",
		"Unclamp Timeout", "", "Clamp retracted not made", "")
	s.advance(30 * time.Second)
	// 2) reset pressed (ack) with the gate OPEN: fault clears, interlock
	//    inter-state — the flip Mike observed; episode root must not move
	s.send(AUTO|RESET, 0, "50", "Unclamp",
		"", "Safety gate 1 not closed", "", "")
	s.advance(60 * time.Second)
	// 3) gate closed, retry runs the faulted step (productive blip)
	s.send(AUTO|RUN|ILKOK, 0, "50", "Unclamp", "", "", "", "")
	s.advance(5 * time.Second)
	// 4) retry fails: re-fault on the same step
	s.send(AUTO|FAULT|STEPFLT|ILKOK, 0, "50", "Unclamp",
		"Unclamp Timeout", "", "Clamp retracted not made", "")
	s.advance(20 * time.Second)
	// 5) second retry works: productive on the NEXT step = recovered
	s.send(AUTO|RUN|ILKOK, 0, "50", "Unclamp", "Unclamp Timeout", "", "", "")
	s.advance(2 * time.Second)
	s.send(AUTO|RUN|ILKOK, 0, "60", "Index", "Unclamp Timeout", "", "", "")

	if len(s.cap.episodes) != 1 {
		t.Fatalf("episodes: %d", len(s.cap.episodes))
	}
	ep := s.cap.episodes[0]
	if ep.ReasonType != model.ReasonStepFault ||
		ep.Reason != "Unclamp Timeout — Clamp retracted not made" {
		t.Fatalf("root cause %q %q", ep.ReasonType, ep.Reason)
	}
	if ep.StepName != "50" {
		t.Fatalf("step %q", ep.StepName)
	}
	// spans fault start to true recovery; the 5s retry blip did NOT end it
	if got := ep.EndTs.Sub(ep.StartTs); got != 117*time.Second {
		t.Fatalf("episode span %v", got)
	}
	if ep.Retries != 1 {
		t.Fatalf("retries %d", ep.Retries)
	}
	if ep.AckTs == nil || ep.AckTs.Sub(ep.StartTs) != 30*time.Second {
		t.Fatalf("ack %v", ep.AckTs)
	}
	// raw intervals kept every phase
	kinds := []string{}
	for _, iv := range s.cap.intervals {
		kinds = append(kinds, iv.State+"/"+iv.ReasonType)
	}
	// (the final productive interval is still open, so it is not captured)
	want := []string{"productive/", "down/step_fault", "down/interlock",
		"productive/", "down/step_fault"}
	if len(kinds) != len(want) {
		t.Fatalf("raw intervals %v", kinds)
	}
	for i := range want {
		if kinds[i] != want[i] {
			t.Fatalf("raw[%d] = %q, want %q (%v)", i, kinds[i], want[i], kinds)
		}
	}
}

func TestEpisodeClosesOnSustainedStandby(t *testing.T) {
	s := newSim()
	s.send(AUTO|RUN|ILKOK, 0, "50", "Unclamp", "", "", "", "")
	s.advance(time.Second)
	s.send(AUTO|FAULT|STEPFLT|ILKOK, 0, "50", "Unclamp", "Unclamp Timeout", "", "", "")
	s.advance(30 * time.Second)
	// operator stops the sequence and walks away: healthy standby
	s.send(AUTO|ILKOK, 0, "50", "Unclamp", "Unclamp Timeout", "", "", "")
	s.advance(30 * time.Second)
	s.send(AUTO|ILKOK, 0, "50", "Unclamp", "Unclamp Timeout", "", "", "")
	s.advance(35 * time.Second)
	s.send(AUTO|ILKOK, 0, "50", "Unclamp", "Unclamp Timeout", "", "", "")
	if len(s.cap.episodes) != 1 {
		t.Fatalf("episodes: %d", len(s.cap.episodes))
	}
	// episode ends where the standby began, not when the 60s grace elapsed
	ep := s.cap.episodes[0]
	if got := ep.EndTs.Sub(ep.StartTs); got != 30*time.Second {
		t.Fatalf("episode span %v", got)
	}
}
