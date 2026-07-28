// Package ingest runs the UDP listener: decode, map by (source IP,
// station, em label), dispatch to the EM tracker. Single goroutine owns
// all trackers — no locks needed at these rates; the store enqueue is
// non-blocking.
package ingest

import (
	"context"
	"fmt"
	"log/slog"
	"net"
	"strings"
	"sync"
	"time"

	"github.com/mike-at-base/equipment_monitor/hub/internal/tracker"
	"github.com/mike-at-base/equipment_monitor/hub/internal/wire"
)

const offlineAfter = 10 * time.Second

// Key identifies a tracker by the v4-declared (line, station, em), all
// lower-cased. There is no source-IP routing — every datagram self-identifies.
type Key struct {
	Line    string
	Station string
	EMLabel string
}

// Discoverer creates+persists a tracker for an EM seen live for the first
// time (v4 auto-registration). Returns the new tracker to add to the map.
type Discoverer func(line, station, emLabel string, wireVer int) (*tracker.EM, error)

type Service struct {
	mu       sync.Mutex
	trackers map[Key]*tracker.EM
	discover Discoverer // nil disables auto-registration
	log      *slog.Logger
	port     int
}

// LiveEM is a lock-safe snapshot of one tracker for the live API.
type LiveEM struct {
	EMID       int       `json:"-"`
	Line       string    `json:"line"`
	Station    string    `json:"station"`
	EMLabel    string    `json:"em_label"`
	State      string    `json:"state"`
	ReasonType string    `json:"reason_type,omitempty"`
	Reason     string    `json:"reason,omitempty"`
	Step       string    `json:"step,omitempty"`
	Since      time.Time `json:"since"`
	LastSeen   time.Time `json:"last_seen"`
	// open down episode (sticky root cause), if any
	EpisodeOpen    bool      `json:"episode_open,omitempty"`
	EpisodeStart   time.Time `json:"episode_start,omitzero"`
	EpisodeRType   string    `json:"episode_reason_type,omitempty"`
	EpisodeReason  string    `json:"episode_reason,omitempty"`
	EpisodeStep    string    `json:"episode_step,omitempty"`
	EpisodeRetries int       `json:"episode_retries,omitempty"`
}

// BitFlag is one named status/mode bit and whether it is currently set.
type BitFlag struct {
	Name string `json:"name"`
	On   bool   `json:"on"`
}

// RawEM is the last decoded datagram for one EM, exposed verbatim for the
// engineering debug view. Kept off /live and the SSE stream (which the site
// overview uses) so those stay lean — this is fetched per-EM on demand.
type RawEM struct {
	EMID           int       `json:"-"`
	Line           string    `json:"line"`
	Station        string    `json:"station"`
	EMLabel        string    `json:"em_label"`
	MsgType        uint8     `json:"msg_type"`
	Seq            uint32    `json:"seq"`
	ActiveSequence int16     `json:"active_sequence"`
	Step           string    `json:"step"`
	StepDesc       string    `json:"step_desc"`
	StepActiveMs   int32     `json:"step_active_ms"`
	StatusBits     uint16    `json:"status_bits"`
	ModeBits       uint16    `json:"mode_bits"`
	Status         []BitFlag `json:"status"`
	Modes          []BitFlag `json:"modes"`
	AlarmMsg       string    `json:"alarm_msg"`
	InterlockFails string    `json:"interlock_fails"`
	FaultConds     string    `json:"fault_conds"`
	WaitingOn      string    `json:"waiting_on"`
	PLCTime        time.Time `json:"plc_time,omitzero"`
	RecvTime       time.Time `json:"recv_time"`
	SkewMs         int64     `json:"skew_ms"`
}

func New(port int, trackers map[Key]*tracker.EM,
	discover Discoverer, log *slog.Logger) *Service {
	return &Service{trackers: trackers, discover: discover, log: log, port: port}
}

// Snapshot returns the current state of every tracked EM (for /api/v2/live).
func (s *Service) Snapshot() []LiveEM {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]LiveEM, 0, len(s.trackers))
	for _, t := range s.trackers {
		le := LiveEM{
			EMID: t.EMID(), Line: t.Line(),
			Station: t.Station(), EMLabel: t.EMLabel(),
			State: t.State(), ReasonType: t.StateReasonType(),
			Reason: t.StateReason(), Step: t.Step(),
			Since: t.StateSince(), LastSeen: t.LastSeen(),
		}
		le.EpisodeOpen, le.EpisodeStart, le.EpisodeRType,
			le.EpisodeReason, le.EpisodeStep, le.EpisodeRetries = t.Episode()
		out = append(out, le)
	}
	return out
}

// RawSnapshot returns the last decoded datagram of every EM that has
// received one, with status/mode bits decoded into named flags. Used by the
// per-EM engineering debug endpoint.
func (s *Service) RawSnapshot() []RawEM {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]RawEM, 0, len(s.trackers))
	for _, t := range s.trackers {
		d, recv := t.Raw()
		if d == nil {
			continue
		}
		re := RawEM{
			EMID: t.EMID(), Line: t.Line(),
			Station: t.Station(), EMLabel: t.EMLabel(),
			MsgType: d.MsgType, Seq: d.Seq, ActiveSequence: d.ActiveSequence,
			Step: d.Step, StepDesc: d.StepDesc, StepActiveMs: d.StepActiveMs,
			StatusBits: d.StatusBits, ModeBits: d.ModeBits,
			AlarmMsg: d.AlarmMsg, InterlockFails: d.InterlockFails,
			FaultConds: d.FaultConds, WaitingOn: d.WaitingOn,
			PLCTime: d.PLCTime, RecvTime: recv,
		}
		for _, f := range wire.StatusFlags {
			re.Status = append(re.Status, BitFlag{f.Name, d.StatusBits&f.Mask != 0})
		}
		for _, f := range wire.ModeFlags {
			re.Modes = append(re.Modes, BitFlag{f.Name, d.ModeBits&f.Mask != 0})
		}
		if !d.PLCTime.IsZero() {
			re.SkewMs = d.PLCTime.Sub(recv).Milliseconds()
		}
		out = append(out, re)
	}
	return out
}

// SetSequences updates the live sequence config for the tracker with emID
// (after an engineer edits/confirms the EM). Returns false if not tracked.
func (s *Service) SetSequences(emID int, seqs map[int16]tracker.SeqConfig) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, t := range s.trackers {
		if t.EMID() == emID {
			t.SetSequences(seqs)
			return true
		}
	}
	return false
}

// RemoveEM drops the tracker for emID (after its EM is deleted). If a
// datagram for it arrives again it simply re-registers.
func (s *Service) RemoveEM(emID int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for k, t := range s.trackers {
		if t.EMID() == emID {
			delete(s.trackers, k)
			return
		}
	}
}

// FlushAll closes all open intervals (graceful shutdown).
func (s *Service) FlushAll(ts time.Time) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, t := range s.trackers {
		t.FlushOpen(ts)
	}
}

func (s *Service) Run(ctx context.Context) error {
	conn, err := net.ListenUDP("udp", &net.UDPAddr{Port: s.port})
	if err != nil {
		return fmt.Errorf("udp listen :%d: %w", s.port, err)
	}
	defer conn.Close()
	s.log.Info("telemetry listener up", "port", s.port, "ems", len(s.trackers))

	go func() {
		<-ctx.Done()
		conn.Close() // unblocks ReadFromUDP
	}()
	go s.offlineSweeper(ctx)

	buf := make([]byte, 2048)
	unknownLogged := map[string]bool{} // dedup "no lineName" warnings by source IP
	for {
		n, addr, err := conn.ReadFromUDP(buf)
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			s.log.Warn("udp read", "err", err)
			continue
		}
		now := time.Now().UTC()
		d, err := wire.Decode(buf[:n])
		if err != nil {
			s.log.Debug("bad datagram", "from", addr.IP.String(), "err", err)
			continue
		}
		// v4 datagrams self-declare their line. A datagram without a lineName
		// is a misconfigured FB (lineName input unset) — drop it and say so.
		if d.LineName == "" {
			if !unknownLogged[addr.IP.String()] {
				unknownLogged[addr.IP.String()] = true
				s.log.Warn("datagram has no lineName — dropped (set the FB's lineName input)",
					"from", addr.IP.String(), "station", d.Station, "em", d.EMLabel)
			}
			continue
		}
		key := Key{strings.ToLower(d.LineName), strings.ToLower(d.Station), strings.ToLower(d.EMLabel)}

		s.mu.Lock()
		t, ok := s.trackers[key]
		if !ok {
			if s.discover == nil {
				s.mu.Unlock()
				continue
			}
			nt, err := s.discover(d.LineName, d.Station, d.EMLabel, int(d.Version))
			if err != nil {
				s.mu.Unlock()
				s.log.Error("auto-register failed",
					"line", d.LineName, "station", d.Station, "em", d.EMLabel, "err", err)
				continue
			}
			s.trackers[key] = nt
			t = nt
			s.log.Info("auto-registered EM (unconfirmed)",
				"line", d.LineName, "station", d.Station, "em", d.EMLabel, "wire", d.Version)
		}
		if gap := t.SeqGap(d.Seq); gap > 0 {
			s.log.Warn("missed datagrams (heartbeat self-heals)",
				"station", d.Station, "em", d.EMLabel, "gap", gap)
		}
		t.Ingest(d, now)
		s.mu.Unlock()
	}
}

func (s *Service) offlineSweeper(ctx context.Context) {
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			now := time.Now().UTC()
			s.mu.Lock()
			for _, t := range s.trackers {
				if last := t.LastSeen(); !last.IsZero() && now.Sub(last) > offlineAfter {
					t.MarkOffline(now)
				}
			}
			s.mu.Unlock()
		}
	}
}
