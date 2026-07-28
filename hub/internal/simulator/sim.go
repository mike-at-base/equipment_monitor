// Package simulator is the built-in, UI-driven EM availability simulator.
//
// It heartbeats a set of fake EMs at the hub's OWN collector port over real
// UDP (wire v4), so simulated telemetry takes the exact same path as a PLC:
// ingest -> tracker -> DB -> composed availability. Built as a training
// tool — teammates flip states from the Simulator page and watch the RBD,
// timelines, and availability react.
package simulator

import (
	"fmt"
	"net"
	"path"
	"strings"
	"sync"
	"time"

	"github.com/mike-at-base/equipment_monitor/hub/internal/wire"
)

// stateBits maps a simulated state name to its wire status bits. down gets
// the alarm text; offline suppresses heartbeats entirely (telemetry lost).
var stateBits = map[string]uint16{
	"up":      wire.BitAutomatic | wire.BitRunning | wire.BitInterlockOk,
	"down":    wire.BitAutomatic | wire.BitFault | wire.BitInterlockOk,
	"standby": wire.BitAutomatic | wire.BitInterlockOk,
	"paused":  wire.BitAutomatic | wire.BitPaused | wire.BitInterlockOk,
	"manual":  wire.BitInterlockOk,
	"offline": 0,
}

// EMState is one simulated EM's commanded state (JSON for the sim API).
type EMState struct {
	Station string `json:"station"`
	Label   string `json:"em_label"`
	State   string `json:"state"`
	Reason  string `json:"reason,omitempty"`
}

// Status is the sim snapshot returned to the UI.
type Status struct {
	Running bool      `json:"running"`
	Line    string    `json:"line"`
	EMs     []EMState `json:"ems"`
}

type simEM struct {
	station, label string
	state          string
	reason         string
	seq            uint32
}

// Sim drives one simulated line. Zero value is a stopped simulator.
type Sim struct {
	target string // collector UDP addr, e.g. 127.0.0.1:15020

	mu   sync.Mutex
	line string
	ems  []*simEM
	stop chan struct{}
}

func New(target string) *Sim { return &Sim{target: target} }

// ParseSpec parses "STATION:em,em;STATION:em,..." into (station,label) pairs.
func ParseSpec(spec string) ([][2]string, error) {
	var out [][2]string
	seen := map[string]bool{}
	for _, part := range strings.Split(spec, ";") {
		st, labels, ok := strings.Cut(strings.TrimSpace(part), ":")
		st = strings.TrimSpace(st)
		if !ok || st == "" {
			return nil, fmt.Errorf("bad spec segment %q (want STATION:em,em)", part)
		}
		for _, l := range strings.Split(labels, ",") {
			l = strings.TrimSpace(l)
			if l == "" {
				continue
			}
			key := strings.ToLower(st + "/" + l)
			if seen[key] {
				return nil, fmt.Errorf("duplicate EM %s/%s", st, l)
			}
			seen[key] = true
			out = append(out, [2]string{st, l})
		}
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("spec defines no EMs")
	}
	if len(out) > 64 {
		return nil, fmt.Errorf("spec defines %d EMs (max 64)", len(out))
	}
	return out, nil
}

// Start (re)starts the simulator with a fresh topology; all EMs begin "up".
func (s *Sim) Start(line, spec string) error {
	pairs, err := ParseSpec(spec)
	if err != nil {
		return err
	}
	if strings.TrimSpace(line) == "" {
		return fmt.Errorf("line name required")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.stop != nil {
		close(s.stop)
	}
	s.line = strings.TrimSpace(line)
	s.ems = nil
	for _, p := range pairs {
		s.ems = append(s.ems, &simEM{station: p[0], label: p[1], state: "up"})
	}
	s.stop = make(chan struct{})
	go s.run(s.stop)
	return nil
}

// Stop halts heartbeats (EMs drift to offline / telemetry lost).
func (s *Sim) Stop() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.stop != nil {
		close(s.stop)
		s.stop = nil
	}
}

// SetState applies a state to every EM matching pattern (glob on the label,
// or STATION/label). Returns how many EMs matched.
func (s *Sim) SetState(pattern, state, reason string) (int, error) {
	if _, ok := stateBits[state]; !ok {
		return 0, fmt.Errorf("unknown state %q", state)
	}
	pat := strings.ToLower(strings.TrimSpace(pattern))
	if pat == "" {
		return 0, fmt.Errorf("pattern required")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	n := 0
	for _, e := range s.ems {
		var ok bool
		if strings.Contains(pat, "/") {
			ok, _ = path.Match(pat, strings.ToLower(e.station+"/"+e.label))
		} else {
			ok, _ = path.Match(pat, strings.ToLower(e.label))
		}
		if !ok {
			continue
		}
		e.state = state
		e.reason = reason
		n++
	}
	if n == 0 {
		return 0, fmt.Errorf("no EM matches %q", pattern)
	}
	return n, nil
}

// Snapshot returns the sim status for the UI.
func (s *Sim) Snapshot() Status {
	s.mu.Lock()
	defer s.mu.Unlock()
	st := Status{Running: s.stop != nil, Line: s.line, EMs: []EMState{}}
	for _, e := range s.ems {
		st.EMs = append(st.EMs, EMState{
			Station: e.station, Label: e.label, State: e.state, Reason: e.reason,
		})
	}
	return st
}

// Line returns the simulated line name ("" when never started).
func (s *Sim) Line() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.line
}

func (s *Sim) run(stop chan struct{}) {
	conn, err := net.Dial("udp", s.target)
	if err != nil {
		return
	}
	defer conn.Close()
	tick := time.NewTicker(time.Second)
	defer tick.Stop()
	for {
		select {
		case <-stop:
			return
		case <-tick.C:
			s.mu.Lock()
			for _, e := range s.ems {
				if e.state == "offline" {
					continue
				}
				alarm := ""
				if e.state == "down" {
					alarm = e.reason
					if alarm == "" {
						alarm = "Simulated fault"
					}
				}
				e.seq++
				_, _ = conn.Write(wire.BuildSim(wire.MsgEvent, stateBits[e.state], 0,
					e.seq, 1, e.station, e.label, "20", "Run", alarm, "", "", "",
					s.line, time.Now()))
			}
			s.mu.Unlock()
		}
	}
}
