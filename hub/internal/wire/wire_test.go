package wire

import (
	"testing"
	"time"
)

func TestDecodeRoundTrip(t *testing.T) {
	now := time.Now().UTC().Truncate(time.Microsecond)
	raw := BuildTest(MsgEvent, BitAutomatic|BitRunning|BitInterlockOk, 0x0008,
		42, 1, "20", "Close clamp", "alarm text", "gate open",
		"cond a; cond b", "waiting text", now)
	if len(raw) != PayloadLen {
		t.Fatalf("payload length %d, want %d", len(raw), PayloadLen)
	}
	d, err := Decode(raw)
	if err != nil {
		t.Fatal(err)
	}
	if d.Station != "ST10000" || d.EMLabel != "main" {
		t.Fatalf("identity: %q/%q", d.Station, d.EMLabel)
	}
	if d.Step != "20" || d.StepDesc != "Close clamp" {
		t.Fatalf("step: %q/%q", d.Step, d.StepDesc)
	}
	if d.Seq != 42 || d.ActiveSequence != 1 || d.StepActiveMs != 5000 {
		t.Fatalf("numerics: seq=%d as=%d ms=%d", d.Seq, d.ActiveSequence, d.StepActiveMs)
	}
	if !d.Bit(BitRunning) || d.Bit(BitFault) || !d.Mode(0x0008) {
		t.Fatalf("bits: %04x/%04x", d.StatusBits, d.ModeBits)
	}
	if d.AlarmMsg != "alarm text" || d.InterlockFails != "gate open" ||
		d.FaultConds != "cond a; cond b" || d.WaitingOn != "waiting text" {
		t.Fatalf("strings: %q %q %q %q", d.AlarmMsg, d.InterlockFails, d.FaultConds, d.WaitingOn)
	}
	if got := d.PLCTime.Sub(now); got > time.Microsecond || got < -time.Microsecond {
		t.Fatalf("plc time delta %v", got)
	}
}

func TestDecodeRejects(t *testing.T) {
	if _, err := Decode(make([]byte, 100)); err == nil {
		t.Fatal("short datagram accepted")
	}
	raw := BuildTest(MsgEvent, 0, 0, 1, 1, "", "", "", "", "", "", time.Now())
	raw[0] = 2 // old wire version
	if _, err := Decode(raw); err == nil {
		t.Fatal("wrong version accepted")
	}
}

func TestDecodeV4LineName(t *testing.T) {
	raw := BuildTestLine(MsgEvent, 0, 0, 1, 1, "20", "", "", "", "", "",
		"MOD1", time.Now())
	if len(raw) != PayloadLen || raw[0] != Version {
		t.Fatalf("v4 payload len=%d ver=%d, want %d/%d", len(raw), raw[0], PayloadLen, Version)
	}
	d, err := Decode(raw)
	if err != nil {
		t.Fatal(err)
	}
	if d.LineName != "MOD1" {
		t.Fatalf("lineName %q, want MOD1", d.LineName)
	}
}

// A v4 datagram truncated to the v3 length must still decode (superset
// layout), with lineName empty — this is the migration path where a legacy
// v3 PLC and a v4 collector coexist.
func TestDecodeV3BackCompat(t *testing.T) {
	raw := BuildTestLine(MsgEvent, BitRunning, 0, 7, 1, "30", "desc", "", "", "", "",
		"MOD1", time.Now())
	v3 := raw[:payloadLenV3]
	v3[0] = 3 // legacy version marker
	d, err := Decode(v3)
	if err != nil {
		t.Fatalf("v3 datagram rejected: %v", err)
	}
	if d.LineName != "" {
		t.Fatalf("v3 lineName should be empty, got %q", d.LineName)
	}
	if d.Step != "30" || !d.Bit(BitRunning) {
		t.Fatalf("v3 core fields wrong: step=%q running=%v", d.Step, d.Bit(BitRunning))
	}
}

// Forward compatibility: a newer PLC (higher version, at least the length this
// build knows) must still decode — the collector reads the fields it knows
// and ignores anything newer appended past them.
func TestDecodeForwardCompat(t *testing.T) {
	raw := BuildTestLine(MsgEvent, 0, 0, 1, 1, "40", "", "", "", "", "",
		"MOD2", time.Now())
	raw[0] = 9                         // pretend a future wire version
	raw = append(raw, 0xAA, 0xBB, 0xCC) // extra trailing bytes we don't understand
	d, err := Decode(raw)
	if err != nil {
		t.Fatalf("future version rejected: %v", err)
	}
	if d.Version != 9 || d.LineName != "MOD2" || d.Step != "40" {
		t.Fatalf("forward decode: ver=%d line=%q step=%q", d.Version, d.LineName, d.Step)
	}
}
