// Package tracker turns telemetry datagrams into derived rows: exclusive
// state intervals (SEMI E10 + deduced flow states), step events, cycles,
// mode windows, and operator events.
//
// This is the Go port of the state logic proven in the Python collector
// (collector/state_tracker.py + udp_receiver.py); the golden tests replay
// the same scenarios that validated the Python side end-to-end.
package tracker

import (
	"strings"
	"time"

	"github.com/mike-at-base/equipment_monitor/hub/internal/model"
	"github.com/mike-at-base/equipment_monitor/hub/internal/wire"
)

// Direction keywords for classifying a healthy dwell when cycle position
// doesn't decide it.
var kwStarved = []string{"present", "available", "upstream", "infeed",
	"supply", "arrive", "loaded", "at fixture", "at nest", "starv"}
var kwBlocked = []string{"downstream", "outfeed", "clear", "free",
	"empty pos", "takeaway", "unload", "exit", "occupied", "full", "block"}

// MaxClockSkew: beyond this, the PLC clock is not trusted and receive time
// is used instead.
const MaxClockSkew = 120 * time.Second

type SeqConfig struct {
	Index         int16
	IsProduction  bool
	CycleStart    string
	CycleComplete string
}

type Config struct {
	EMID      int
	Station   string
	EMLabel   string
	Sequences map[int16]SeqConfig
}

type EM struct {
	cfg   Config
	store model.Store

	lastSeen time.Time
	lastSeq  uint32
	haveSeq  bool

	// last decoded datagram + its receive time (for the raw debug view)
	last     *wire.Datagram
	lastRecv time.Time

	// current exclusive state interval (open)
	curState  string
	curReason string
	curRType  string
	curSeqIdx int16
	curStep   string
	curStart  time.Time
	curAck    *time.Time
	resetPrev bool

	// step tracking (active sequence)
	stepName    string
	stepDesc    string
	stepSeq     int16
	stepStart   time.Time
	stepFaulted bool

	// cycle tracking
	cyclePhase  map[int16]string // "work" | "exchange"
	cycleStart  map[int16]time.Time
	cycleWork   map[int16]*time.Time
	cycleDirty  map[int16]bool // interrupted flag
	cycleOpen   map[int16]bool

	// mode windows
	modeStart map[string]time.Time

	// down episode (sticky root cause; see model.DownEpisode)
	epOpen         bool
	epStart        time.Time
	epRType        string
	epReason       string
	epSeq          int16
	epStep         string
	epAck          *time.Time
	epRetries      int
	epDownMs       int64
	epStandbySince time.Time
	prevApplied    string
}

func New(cfg Config, store model.Store) *EM {
	return &EM{
		cfg:        cfg,
		store:      store,
		cyclePhase: map[int16]string{},
		cycleStart: map[int16]time.Time{},
		cycleWork:  map[int16]*time.Time{},
		cycleDirty: map[int16]bool{},
		cycleOpen:  map[int16]bool{},
		modeStart:  map[string]time.Time{},
	}
}

func (t *EM) LastSeen() time.Time   { return t.lastSeen }
func (t *EM) EMID() int             { return t.cfg.EMID }
func (t *EM) StateSince() time.Time { return t.curStart }
func (t *EM) StateReasonType() string { return t.curRType }
func (t *EM) State() string       { return t.curState }
func (t *EM) StateReason() string { return t.curReason }
func (t *EM) Step() string        { return t.stepName }
func (t *EM) Station() string     { return t.cfg.Station }
func (t *EM) EMLabel() string     { return t.cfg.EMLabel }

func (t *EM) Episode() (open bool, start time.Time, rtype, reason, step string, retries int) {
	return t.epOpen, t.epStart, t.epRType, t.epReason, t.epStep, t.epRetries
}

// Raw returns the last decoded datagram and its receive time (nil until the
// first datagram). Used by the engineering debug view.
func (t *EM) Raw() (*wire.Datagram, time.Time) { return t.last, t.lastRecv }

// SeqGap reports datagrams missed since the last one (0 when none/unknown).
func (t *EM) SeqGap(seq uint32) int {
	if !t.haveSeq {
		return 0
	}
	gap := int(int64(seq) - int64(t.lastSeq) - 1)
	if gap < 0 || gap > 100000 { // wrap / PLC restart
		return 0
	}
	return gap
}

// Ingest applies one datagram. recvTime is the collector receive time; the
// PLC timestamp is used when plausible.
func (t *EM) Ingest(d *wire.Datagram, recvTime time.Time) {
	ts := recvTime
	if !d.PLCTime.IsZero() {
		skew := d.PLCTime.Sub(recvTime)
		if skew < MaxClockSkew && skew > -MaxClockSkew {
			ts = d.PLCTime
		}
	}
	t.lastSeen = recvTime
	t.last, t.lastRecv = d, recvTime
	t.lastSeq, t.haveSeq = d.Seq, true

	alarmActive := d.Bit(wire.BitFault) || d.Bit(wire.BitStepFault) || d.Bit(wire.BitExtAlarm)

	t.trackSteps(d, ts)
	t.trackModes(d, ts)

	state, rtype, reason := t.classify(d, alarmActive)
	t.applyState(state, rtype, reason, d, ts)
	t.trackReset(d, ts)
}

// FlushOpen closes every open interval at shutdown so a restart doesn't
// lose the state the machine was in (the successor process reopens
// tracking from the next datagram).
func (t *EM) FlushOpen(ts time.Time) {
	t.closeInterval(ts)
	if t.epOpen {
		t.closeEpisode(ts)
	}
	for flag, start := range t.modeStart {
		t.store.AddModeInterval(model.ModeInterval{
			EMID: t.cfg.EMID, Flag: flag, StartTs: start, EndTs: ts,
		})
		delete(t.modeStart, flag)
	}
}

// MarkOffline closes the current interval into "offline" after a heartbeat
// gap; the next datagram reopens normal tracking.
func (t *EM) MarkOffline(ts time.Time) {
	if t.curState == "" || t.curState == model.StateOffline {
		return
	}
	t.closeInterval(ts)
	t.openInterval(model.StateOffline, "", "telemetry lost", ts)
}

// ── steps & cycles ───────────────────────────────────────────────────────

func (t *EM) trackSteps(d *wire.Datagram, ts time.Time) {
	seq := d.ActiveSequence
	if seq <= 0 {
		return
	}
	if _, known := t.cfg.Sequences[seq]; !known {
		return
	}
	if d.Bit(wire.BitStepFault) {
		t.stepFaulted = true
	}
	if d.Step == t.stepName && seq == t.stepSeq {
		return
	}
	// close the previous step
	if t.stepName != "" {
		t.store.AddStepEvent(model.StepEvent{
			EMID: t.cfg.EMID, SeqIndex: t.stepSeq,
			StepName: t.stepName, StepDesc: t.stepDesc,
			StartTs: t.stepStart, EndTs: ts,
			DurationMs: ts.Sub(t.stepStart).Milliseconds(),
			WasFaulted: t.stepFaulted,
		})
	}
	t.stepName, t.stepDesc, t.stepSeq = d.Step, d.StepDesc, seq
	t.stepStart, t.stepFaulted = ts, false
	t.trackCycleEdges(seq, d.Step, ts)
}

func (t *EM) trackCycleEdges(seq int16, step string, ts time.Time) {
	sc := t.cfg.Sequences[seq]
	if sc.CycleStart == "" {
		return
	}
	switch step {
	case sc.CycleStart:
		if t.cycleOpen[seq] {
			start := t.cycleStart[seq]
			c := model.Cycle{
				EMID: t.cfg.EMID, SeqIndex: seq,
				StartTs: start, EndTs: ts,
				TotalMs:     ts.Sub(start).Milliseconds(),
				WorkEndTs:   t.cycleWork[seq],
				Interrupted: t.cycleDirty[seq],
			}
			if c.WorkEndTs != nil {
				w := c.WorkEndTs.Sub(start).Milliseconds()
				e := ts.Sub(*c.WorkEndTs).Milliseconds()
				c.WorkMs, c.ExchangeMs = &w, &e
			}
			t.store.AddCycle(c)
		}
		t.cycleOpen[seq] = true
		t.cycleStart[seq] = ts
		t.cycleWork[seq] = nil
		t.cycleDirty[seq] = false
		t.cyclePhase[seq] = "work"
	case sc.CycleComplete:
		if sc.CycleComplete != "" {
			t.cyclePhase[seq] = "exchange"
			if t.cycleOpen[seq] && t.cycleWork[seq] == nil {
				w := ts
				t.cycleWork[seq] = &w
			}
		}
	}
}

// ── modes ────────────────────────────────────────────────────────────────

func (t *EM) trackModes(d *wire.Datagram, ts time.Time) {
	for _, f := range wire.ModeFlags {
		active := d.Mode(f.Mask)
		start, open := t.modeStart[f.Name]
		if active && !open {
			t.modeStart[f.Name] = ts
		} else if !active && open {
			t.store.AddModeInterval(model.ModeInterval{
				EMID: t.cfg.EMID, Flag: f.Name, StartTs: start, EndTs: ts,
			})
			delete(t.modeStart, f.Name)
		}
	}
}

// ── operator reset / MTTR ack ────────────────────────────────────────────

func (t *EM) trackReset(d *wire.Datagram, ts time.Time) {
	reset := d.Bit(wire.BitReset)
	if reset && !t.resetPrev {
		t.store.AddOperatorEvent(model.OperatorEvent{
			EMID: t.cfg.EMID, Ts: ts, Event: "reset",
		})
		if t.curState == model.StateDown && t.curAck == nil {
			ack := ts
			t.curAck = &ack
		}
		if t.epOpen && t.epAck == nil {
			ack := ts
			t.epAck = &ack
		}
	}
	t.resetPrev = reset
}

// ── state classification ─────────────────────────────────────────────────

func (t *EM) classify(d *wire.Datagram, alarmActive bool) (state, rtype, reason string) {
	auto := d.Bit(wire.BitAutomatic)
	fault := d.Bit(wire.BitFault)
	running := d.Bit(wire.BitRunning)
	paused := d.Bit(wire.BitPaused)
	ilkOk := d.Bit(wire.BitInterlockOk)

	// The PLC never clears status.alarm.message; trust it only while an
	// alarm bit is asserted (same rule as the Python receiver).
	alarmMsg := ""
	if alarmActive {
		alarmMsg = d.AlarmMsg
	}

	switch {
	case fault:
		if d.Bit(wire.BitStepFault) {
			return model.StateDown, model.ReasonStepFault,
				composeFaultReason(alarmMsg, d.FaultConds)
		}
		if !ilkOk && d.InterlockFails != "" {
			return model.StateDown, model.ReasonInterlock, d.InterlockFails
		}
		if alarmMsg != "" {
			return model.StateDown, model.ReasonFault, alarmMsg
		}
		return model.StateDown, model.ReasonFault, "EM fault"
	case !ilkOk:
		// A failing interlock is UNAVAILABLE with the failing conditions as
		// the reason, regardless of the mode the machine landed in — an
		// interlock trip often drops the EM out of automatic, and that must
		// not be recorded as reasonless "manual" time.
		reason := d.InterlockFails
		if reason == "" {
			reason = "Interlock not OK"
		}
		return model.StateDown, model.ReasonInterlock, reason
	case !auto:
		return model.StateManual, "", "Manual mode"
	case running && d.WaitingOn != "":
		kind := t.classifyWait(d.ActiveSequence, d.Step, d.WaitingOn)
		return kind, model.ReasonFlow, d.WaitingOn
	case running:
		return model.StateProductive, "", ""
	case paused:
		return model.StatePaused, model.ReasonPause, "Operator pause"
	default:
		return model.StateStandby, "", ""
	}
}

func composeFaultReason(alarmMsg, conds string) string {
	switch {
	case alarmMsg != "" && conds != "":
		return alarmMsg + " — " + conds
	case alarmMsg != "":
		return alarmMsg
	case conds != "":
		return conds
	default:
		return "Step fault"
	}
}

func (t *EM) classifyWait(seq int16, step, waitingOn string) string {
	sc, known := t.cfg.Sequences[seq]
	if known && sc.CycleStart != "" && step == sc.CycleStart {
		return model.StateStarved
	}
	if known && sc.CycleComplete != "" {
		switch t.cyclePhase[seq] {
		case "exchange":
			return model.StateBlocked
		case "work":
			if k := keywordKind(waitingOn); k != "" {
				return k
			}
			return model.StateProcessWait
		}
	}
	if k := keywordKind(waitingOn); k != "" {
		return k
	}
	return model.StateWait
}

func keywordKind(waitingOn string) string {
	text := strings.ToLower(waitingOn)
	starved, blocked := false, false
	for _, k := range kwStarved {
		if strings.Contains(text, k) {
			starved = true
			break
		}
	}
	for _, k := range kwBlocked {
		if strings.Contains(text, k) {
			blocked = true
			break
		}
	}
	switch {
	case blocked && !starved:
		return model.StateBlocked
	case starved && !blocked:
		return model.StateStarved
	default:
		return ""
	}
}

// ── interval management ──────────────────────────────────────────────────

func (t *EM) applyState(state, rtype, reason string, d *wire.Datagram, ts time.Time) {
	// production seq gate: flow states only count on production sequences
	if sc, ok := t.cfg.Sequences[d.ActiveSequence]; ok && !sc.IsProduction {
		if state == model.StateStarved || state == model.StateBlocked ||
			state == model.StateProcessWait || state == model.StateWait {
			state, rtype, reason = model.StateProductive, "", ""
		}
	}

	if state != model.StateProductive && state != model.StateStandby {
		// any non-run state inside an open cycle marks it interrupted
		if d.ActiveSequence > 0 && t.cycleOpen[d.ActiveSequence] &&
			state != model.StateStarved && state != model.StateBlocked &&
			state != model.StateProcessWait && state != model.StateWait {
			t.cycleDirty[d.ActiveSequence] = true
		}
	}

	if t.curState == "" {
		t.openInterval(state, rtype, reason, ts)
		t.curSeqIdx, t.curStep = d.ActiveSequence, d.Step
	} else if state == t.curState && rtype == t.curRType {
		// richer reason arriving while open (e.g. fault text on the next
		// datagram, or the waiting-on set changing mid-wait)
		if reason != "" && reason != t.curReason {
			t.curReason = reason
		}
	} else {
		// state OR reason-type changed: close and reopen so the raw
		// timeline shows every phase (fault, gate interlock, retry, ...)
		t.closeInterval(ts)
		t.openInterval(state, rtype, reason, ts)
		t.curSeqIdx, t.curStep = d.ActiveSequence, d.Step
	}
	t.trackEpisode(state, rtype, reason, d, ts)
}

// trackEpisode maintains the sticky-root-cause downtime episode across
// inter-states (gate interlocks, manual, retry-productive blips).
func (t *EM) trackEpisode(state, rtype, reason string, d *wire.Datagram, ts time.Time) {
	prev := t.prevApplied
	t.prevApplied = state

	if state == model.StateDown {
		t.epStandbySince = time.Time{}
		if !t.epOpen {
			t.epOpen, t.epStart = true, ts
			t.epRType, t.epReason = rtype, reason
			t.epSeq, t.epStep = d.ActiveSequence, ""
			if rtype == model.ReasonStepFault {
				t.epStep = d.Step
			}
			t.epAck, t.epRetries, t.epDownMs = nil, 0, 0
			return
		}
		if prev != model.StateDown {
			// re-entered down after a recovery attempt: retry is not uptime
			t.epRetries++
		}
		if t.epRType != model.ReasonStepFault && rtype == model.ReasonStepFault {
			// promote: the sequence fault is the real root cause
			t.epRType, t.epReason = rtype, reason
			t.epSeq, t.epStep = d.ActiveSequence, d.Step
		}
		return
	}
	if !t.epOpen {
		return
	}
	switch state {
	case model.StateProductive:
		t.epStandbySince = time.Time{}
		// recovered only when producing on a DIFFERENT step than the one
		// that faulted; running the faulted step again is a retry
		if t.epStep == "" || d.ActiveSequence != t.epSeq || d.Step != t.epStep {
			t.closeEpisode(ts)
		}
	case model.StateStandby:
		// sustained healthy standby (abandoned job) ends the episode at
		// the point standby began
		if t.epStandbySince.IsZero() {
			t.epStandbySince = ts
		} else if ts.Sub(t.epStandbySince) >= 60*time.Second {
			t.closeEpisode(t.epStandbySince)
		}
	default:
		t.epStandbySince = time.Time{}
	}
}

func (t *EM) closeEpisode(ts time.Time) {
	t.store.AddDownEpisode(model.DownEpisode{
		EMID: t.cfg.EMID, StartTs: t.epStart, EndTs: ts,
		ReasonType: t.epRType, Reason: t.epReason,
		SeqIndex: t.epSeq, StepName: t.epStep,
		AckTs: t.epAck, Retries: t.epRetries, DownMs: t.epDownMs,
	})
	t.epOpen = false
	t.epStandbySince = time.Time{}
}

func (t *EM) openInterval(state, rtype, reason string, ts time.Time) {
	t.curState, t.curRType, t.curReason = state, rtype, reason
	t.curStart, t.curAck = ts, nil
}

func (t *EM) closeInterval(ts time.Time) {
	if t.curState == "" {
		return
	}
	if t.curState == model.StateDown && t.epOpen {
		t.epDownMs += ts.Sub(t.curStart).Milliseconds()
	}
	t.store.AddStateInterval(model.StateInterval{
		EMID: t.cfg.EMID, State: t.curState,
		ReasonType: t.curRType, Reason: t.curReason,
		SeqIndex: t.curSeqIdx, StepName: t.curStep,
		StartTs: t.curStart, EndTs: ts, AckTs: t.curAck,
	})
	t.curState = ""
}
