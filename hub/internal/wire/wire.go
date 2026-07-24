// Package wire decodes EquipmentModuleTelemetry datagrams (wire v3).
//
// The layout is the byte-exact contract with the PLC FB's Serialize output
// (S7 standard layout, big-endian, strings as [max][len][chars...]).
// See ../../plc/TELEMETRY.md. Bump Version together with the UDT.
package wire

import (
	"encoding/binary"
	"fmt"
	"time"
)

const (
	Version      = 4    // current wire format
	PayloadLen   = 1142 // v4 = v3 (1108) + lineName String[32], appended
	payloadLenV3 = 1108 // legacy: no lineName; collector routes by source IP
)

// statusBits
const (
	BitAutomatic   = 0x0001
	BitFault       = 0x0002
	BitRunning     = 0x0004
	BitPaused      = 0x0008
	BitStopped     = 0x0010
	BitUnknown     = 0x0020
	BitStepFault   = 0x0040
	BitInterlockOk = 0x0080
	BitExtAlarm    = 0x0100
	BitReset       = 0x0200
)

// statusBits, in wire order (for the raw debug view)
var StatusFlags = []struct {
	Name string
	Mask uint16
}{
	{"automatic", BitAutomatic},
	{"fault", BitFault},
	{"running", BitRunning},
	{"paused", BitPaused},
	{"stopped", BitStopped},
	{"unknown", BitUnknown},
	{"step_fault", BitStepFault},
	{"interlock_ok", BitInterlockOk},
	{"ext_alarm", BitExtAlarm},
	{"reset", BitReset},
}

// modeBits, in wire order
var ModeFlags = []struct {
	Name string
	Mask uint16
}{
	{"idle", 0x0001},
	{"step_mode", 0x0002},
	{"mes_bypass", 0x0004},
	{"dry_cycle", 0x0008},
	{"end_of_cycle", 0x0010},
	{"pause_at_home", 0x0020},
	{"request_entry", 0x0040},
}

const (
	MsgEvent     = 1
	MsgHeartbeat = 2
)

type Datagram struct {
	Version        uint8 // wire version byte (informational / fleet visibility)
	MsgType        uint8
	StatusBits     uint16
	ModeBits       uint16
	ActiveSequence int16
	Seq            uint32
	StepActiveMs   int32
	PLCTime        time.Time // zero if PLC clock unset
	Station        string
	EMLabel        string
	LineName       string // v4: PLC-declared line/site id ("" on legacy v3)
	Step           string
	StepDesc       string
	AlarmMsg       string
	InterlockFails string
	FaultConds     string
	WaitingOn      string
}

func (d *Datagram) Bit(mask uint16) bool  { return d.StatusBits&mask != 0 }
func (d *Datagram) Mode(mask uint16) bool { return d.ModeBits&mask != 0 }

func s7String(b []byte, off, max int) string {
	cur := int(b[off+1])
	if cur > max {
		cur = max
	}
	raw := b[off+2 : off+2+cur]
	// S7 strings are latin-1; datagram content is ASCII in practice
	out := make([]rune, len(raw))
	for i, c := range raw {
		out[i] = rune(c)
	}
	return trimSpace(string(out))
}

func trimSpace(s string) string {
	start, end := 0, len(s)
	for start < end && (s[start] == ' ' || s[start] == 0) {
		start++
	}
	for end > start && (s[end-1] == ' ' || s[end-1] == 0) {
		end--
	}
	return s[start:end]
}

// Decode parses one telemetry datagram. Returns an error for short buffers
// or wrong wire versions (collector logs and drops).
func Decode(b []byte) (*Datagram, error) {
	if len(b) < payloadLenV3 {
		return nil, fmt.Errorf("short datagram: %d bytes", len(b))
	}
	ver := b[0]
	if ver < 3 {
		return nil, fmt.Errorf("unsupported wire version %d (min 3)", ver)
	}
	d := &Datagram{
		Version:        ver,
		MsgType:        b[1],
		StatusBits:     binary.BigEndian.Uint16(b[2:]),
		ModeBits:       binary.BigEndian.Uint16(b[4:]),
		ActiveSequence: int16(binary.BigEndian.Uint16(b[6:])),
		Seq:            binary.BigEndian.Uint32(b[8:]),
		StepActiveMs:   int32(binary.BigEndian.Uint32(b[12:])),
		Station:        s7String(b, 24, 32),
		EMLabel:        s7String(b, 58, 16),
		Step:           s7String(b, 76, 60),
		StepDesc:       s7String(b, 138, 200),
		AlarmMsg:       s7String(b, 340, 200),
		InterlockFails: s7String(b, 542, 160),
		FaultConds:     s7String(b, 704, 200),
		WaitingOn:      s7String(b, 906, 200),
	}
	ns := binary.BigEndian.Uint64(b[16:])
	if ns > 0 {
		d.PLCTime = time.Unix(0, int64(ns)).UTC()
	}
	// Append-only layout: decode trailing fields when the datagram is long
	// enough to hold them. A newer PLC (higher version, longer payload) is
	// read up to the fields this build knows and the rest ignored; an older
	// one simply lacks them. Keeps the collector forward- and
	// backward-compatible across FB versions without a per-version allowlist.
	if len(b) >= PayloadLen {
		d.LineName = s7String(b, 1108, 32)
	}
	return d, nil
}

// BuildTest constructs a byte-exact datagram the same way the PLC Serialize
// does. Exported for tracker tests; emits a v4 datagram with an empty
// lineName. Use BuildTestLine to set the line id.
func BuildTest(msgType uint8, bits, modes uint16, seq uint32, activeSeq int16,
	step, desc, alarm, ilk, cond, waiting string, plcTime time.Time) []byte {
	return BuildTestLine(msgType, bits, modes, seq, activeSeq,
		step, desc, alarm, ilk, cond, waiting, "", plcTime)
}

// BuildTestLine is BuildTest with an explicit lineName (wire v4 field).
func BuildTestLine(msgType uint8, bits, modes uint16, seq uint32, activeSeq int16,
	step, desc, alarm, ilk, cond, waiting, lineName string, plcTime time.Time) []byte {

	s7s := func(text string, max int) []byte {
		if len(text) > max {
			text = text[:max]
		}
		out := make([]byte, max+2)
		out[0] = byte(max)
		out[1] = byte(len(text))
		copy(out[2:], text)
		return out
	}
	b := make([]byte, 0, PayloadLen)
	head := make([]byte, 24)
	head[0] = Version
	head[1] = msgType
	binary.BigEndian.PutUint16(head[2:], bits)
	binary.BigEndian.PutUint16(head[4:], modes)
	binary.BigEndian.PutUint16(head[6:], uint16(activeSeq))
	binary.BigEndian.PutUint32(head[8:], seq)
	binary.BigEndian.PutUint32(head[12:], 5000) // stepActiveTime ms
	binary.BigEndian.PutUint64(head[16:], uint64(plcTime.UnixNano()))
	b = append(b, head...)
	b = append(b, s7s("ST10000", 32)...)
	b = append(b, s7s("main", 16)...)
	b = append(b, s7s(step, 60)...)
	b = append(b, s7s(desc, 200)...)
	b = append(b, s7s(alarm, 200)...)
	b = append(b, s7s(ilk, 160)...)
	b = append(b, s7s(cond, 200)...)
	b = append(b, s7s(waiting, 200)...)
	b = append(b, s7s(lineName, 32)...) // v4: appended after waitingOn
	return b
}
