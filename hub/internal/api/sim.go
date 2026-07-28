package api

// Built-in simulator endpoints: the Simulator page starts a fake line,
// flips EM states, and tears the sandbox down. Simulated telemetry enters
// through the real UDP collector, so everything downstream is authentic.

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"

	"github.com/mike-at-base/equipment_monitor/hub/internal/simulator"
)

// SetSim attaches the built-in simulator (nil = endpoints report 404).
func (s *Server) SetSim(sim *simulator.Sim) { s.sim = sim }

func (s *Server) simOr404(w http.ResponseWriter) bool {
	if s.sim == nil {
		httpErr(w, 404, fmt.Errorf("simulator not available"))
		return false
	}
	return true
}

func (s *Server) handleGetSim(w http.ResponseWriter, _ *http.Request) {
	if !s.simOr404(w) {
		return
	}
	writeJSON(w, s.sim.Snapshot())
}

// PUT /api/v2/sim {line, spec} — start or replace the simulated topology.
func (s *Server) handleStartSim(w http.ResponseWriter, r *http.Request) {
	if !s.simOr404(w) {
		return
	}
	var body struct {
		Line string `json:"line"`
		Spec string `json:"spec"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		httpErr(w, 400, err)
		return
	}
	// refuse to impersonate a real (non-sim) line that already has EMs
	if l := s.findLine(body.Line); l != nil && !strings.EqualFold(body.Line, s.sim.Line()) {
		httpErr(w, 400, fmt.Errorf("line %q already exists — pick a fresh name for the sandbox", body.Line))
		return
	}
	if err := s.sim.Start(body.Line, body.Spec); err != nil {
		httpErr(w, 400, err)
		return
	}
	writeJSON(w, s.sim.Snapshot())
}

// PUT /api/v2/sim/state {pattern, state, reason}
func (s *Server) handleSimState(w http.ResponseWriter, r *http.Request) {
	if !s.simOr404(w) {
		return
	}
	var body struct {
		Pattern string `json:"pattern"`
		State   string `json:"state"`
		Reason  string `json:"reason"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		httpErr(w, 400, err)
		return
	}
	n, err := s.sim.SetState(body.Pattern, body.State, body.Reason)
	if err != nil {
		httpErr(w, 400, err)
		return
	}
	writeJSON(w, map[string]any{"ok": true, "matched": n})
}

// DELETE /api/v2/sim[?purge=1] — stop the sim; purge also deletes the
// sandbox line's EMs (history included) via the normal delete path.
func (s *Server) handleStopSim(w http.ResponseWriter, r *http.Request) {
	if !s.simOr404(w) {
		return
	}
	s.sim.Stop()
	purged := 0
	if r.URL.Query().Get("purge") == "1" {
		line := s.sim.Line()
		if l := s.findLine(line); l != nil && s.onDelete != nil {
			for _, em := range l.EMs() {
				s.onDelete(em.ID)
				purged++
			}
		}
	}
	writeJSON(w, map[string]any{"ok": true, "purged": purged})
}
