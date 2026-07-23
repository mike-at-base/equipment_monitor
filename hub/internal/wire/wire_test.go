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
