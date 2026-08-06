// Package model holds the derived-row types the tracker emits and the
// Store interface the batch writer implements. Everything is append-only:
// the tracker keeps open intervals in memory and emits rows only when they
// close, so the write path never UPDATEs.
package model

import "time"

// EM states (state_interval.state)
const (
	StateProductive  = "productive"
	StateStandby     = "standby"
	StatePaused      = "paused"
	StateManual      = "manual"
	StateDown        = "down"
	StateStarved     = "starved"
	StateBlocked     = "blocked"
	StateProcessWait = "process_wait"
	StateWait        = "wait"
	StateOffline     = "offline"
)

// down reason types (state_interval.reason_type)
const (
	ReasonStepFault = "step_fault"
	ReasonInterlock = "interlock"
	ReasonFault     = "fault"
	ReasonPause     = "paused"
	ReasonFlow      = "flow"
)

type StateInterval struct {
	EMID       int
	State      string
	ReasonType string
	Reason     string
	SeqIndex   int16
	StepName   string
	StartTs    time.Time
	EndTs      time.Time
	AckTs      *time.Time // first operator reset while down
}

type StepEvent struct {
	EMID       int
	SeqIndex   int16
	StepName   string
	StepDesc   string
	StartTs    time.Time
	EndTs      time.Time
	DurationMs int64
	WasFaulted bool
	// v5: nextStep of the branch the sequencer actually took out of this
	// step. "" when the PLC is pre-v5, or when the step ended without any
	// branch satisfying (forced jump, fault, reset).
	BranchTaken string
}

type Cycle struct {
	EMID        int
	SeqIndex    int16
	StartTs     time.Time
	WorkEndTs   *time.Time // entry into cycle_complete_step
	EndTs       time.Time  // entry into next cycle_start_step
	WorkMs      *int64
	ExchangeMs  *int64
	TotalMs     int64
	Interrupted bool // down/manual occurred within the cycle
}

type ModeInterval struct {
	EMID    int
	Flag    string // idle | step_mode | mes_bypass | dry_cycle | ...
	StartTs time.Time
	EndTs   time.Time
}

// DownEpisode is the sticky-root-cause downtime record: opens at the
// first down interval, absorbs inter-states (gate interlocks, manual,
// retry-productive blips on the faulted step), and closes only when the
// machine is genuinely producing again. Reporting (availability, MTTR,
// alarm pareto) reads episodes; timelines read raw state intervals.
type DownEpisode struct {
	EMID       int
	StartTs    time.Time
	EndTs      time.Time
	ReasonType string // root cause
	Reason     string
	SeqIndex   int16
	StepName   string // faulted step ("" for non-sequence episodes)
	AckTs      *time.Time
	Retries    int   // down re-entries after recovery attempts
	DownMs     int64 // raw down time within the episode
}

type OperatorEvent struct {
	EMID  int
	Ts    time.Time
	Event string // "reset"
}

// Store is what the tracker writes to. The pg implementation batches;
// tests use an in-memory capture.
type Store interface {
	AddStateInterval(StateInterval)
	AddStepEvent(StepEvent)
	AddCycle(Cycle)
	AddModeInterval(ModeInterval)
	AddOperatorEvent(OperatorEvent)
	AddDownEpisode(DownEpisode)
}
