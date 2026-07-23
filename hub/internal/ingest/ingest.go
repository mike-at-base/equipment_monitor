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

type Key struct {
	Host    string
	Station string
	EMLabel string
}

type Service struct {
	mu       sync.Mutex
	trackers map[Key]*tracker.EM
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

func New(port int, trackers map[Key]*tracker.EM, log *slog.Logger) *Service {
	return &Service{trackers: trackers, log: log, port: port}
}

// Snapshot returns the current state of every tracked EM (for /api/v2/live).
func (s *Service) Snapshot(lineByHost map[string]string) []LiveEM {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]LiveEM, 0, len(s.trackers))
	for k, t := range s.trackers {
		le := LiveEM{
			EMID: t.EMID(), Line: lineByHost[k.Host],
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
func (s *Service) RawSnapshot(lineByHost map[string]string) []RawEM {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]RawEM, 0, len(s.trackers))
	for k, t := range s.trackers {
		d, recv := t.Raw()
		if d == nil {
			continue
		}
		re := RawEM{
			EMID: t.EMID(), Line: lineByHost[k.Host],
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
	unknownLogged := map[Key]bool{}
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
		key := Key{addr.IP.String(), strings.ToLower(d.Station), strings.ToLower(d.EMLabel)}
		t, ok := s.trackers[key]
		if !ok {
			if !unknownLogged[key] {
				unknownLogged[key] = true
				s.log.Warn("datagram for unconfigured EM",
					"from", key.Host, "station", d.Station, "em", d.EMLabel)
			}
			continue
		}
		s.mu.Lock()
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
