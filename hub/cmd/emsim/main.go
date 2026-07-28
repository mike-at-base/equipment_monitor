// emsim: interactive EM availability simulator.
//
// Heartbeats a set of fake EMs at the collector (wire v4) and lets you flip
// their states from the console while watching the SCADA react — built for
// exercising composed (k-of-n) availability models, schedules, and the RBD.
//
//	go run ./cmd/emsim                                  # default ST34000 demo set
//	go run ./cmd/emsim -line RBD1 -spec "ST34000:main,ROB01,ROB02,MAG01,MAG02,MAG03,MAG04,MAG05,MAG06,MAG07,MAG08"
//	go run ./cmd/emsim -spec "ST10:main;ST20:main,left,right"
//
// Commands (stdin):
//	list                     show every EM and its simulated state
//	down <pat> [reason...]   fault (composition-down)
//	up <pat>                 automatic + running (productive)
//	standby <pat>            automatic, not running (up for availability)
//	pause <pat>              paused (up for availability)
//	manual <pat>             out of automatic (composition-DOWN)
//	offline <pat>            stop heartbeating (no data -> composition-DOWN)
//	online <pat>             resume heartbeating
//	quit
//
// <pat> matches EM labels, station-qualified (ST34000/MAG01) or bare
// (MAG01), with * globs: `down MAG*`, `up *`.
package main

import (
	"bufio"
	"encoding/binary"
	"flag"
	"fmt"
	"net"
	"os"
	"path"
	"strings"
	"sync"
	"time"

	"github.com/mike-at-base/equipment_monitor/hub/internal/wire"
)

type simState struct {
	bits    uint16
	alarm   string
	offline bool
}

var states = map[string]simState{
	"up":      {bits: wire.BitAutomatic | wire.BitRunning | wire.BitInterlockOk},
	"down":    {bits: wire.BitAutomatic | wire.BitFault | wire.BitInterlockOk, alarm: "Simulated fault"},
	"standby": {bits: wire.BitAutomatic | wire.BitInterlockOk},
	"pause":   {bits: wire.BitAutomatic | wire.BitPaused | wire.BitInterlockOk},
	"manual":  {bits: wire.BitInterlockOk},
	"offline": {offline: true},
}

type em struct {
	station, label string
	state          string // key into states
	alarm          string
	seq            uint32
}

func (e *em) key() string { return e.station + "/" + e.label }

func main() {
	host := flag.String("host", "127.0.0.1:15020", "collector UDP address")
	line := flag.String("line", "RBD1", "line name (wire v4)")
	spec := flag.String("spec", "ST34000:main,ROB01,ROB02,MAG01,MAG02,MAG03,MAG04,MAG05,MAG06,MAG07,MAG08",
		"stations and EMs: STATION:em,em;STATION:em,...")
	flag.Parse()

	var ems []*em
	for _, stPart := range strings.Split(*spec, ";") {
		st, labels, ok := strings.Cut(strings.TrimSpace(stPart), ":")
		if !ok {
			fmt.Printf("bad spec segment %q (want STATION:em,em)\n", stPart)
			os.Exit(2)
		}
		for _, l := range strings.Split(labels, ",") {
			ems = append(ems, &em{station: strings.TrimSpace(st),
				label: strings.TrimSpace(l), state: "up"})
		}
	}

	conn, err := net.Dial("udp", *host)
	if err != nil {
		fmt.Println("dial:", err)
		os.Exit(1)
	}
	defer conn.Close()

	var mu sync.Mutex
	// 1 Hz heartbeat of every online EM's current state
	go func() {
		for range time.Tick(time.Second) {
			mu.Lock()
			for _, e := range ems {
				st := states[e.state]
				if st.offline {
					continue
				}
				alarm := e.alarm
				if alarm == "" {
					alarm = st.alarm
				}
				if e.state != "down" {
					alarm = ""
				}
				e.seq++
				_, _ = conn.Write(datagram(st.bits, e.seq, e.station, e.label, alarm, *line))
			}
			mu.Unlock()
		}
	}()

	fmt.Printf("emsim: %d EMs on line %s -> %s\n", len(ems), *line, *host)
	fmt.Println("commands: list | up|down|standby|pause|manual|offline|online <pat> [reason] | quit")
	sc := bufio.NewScanner(os.Stdin)
	for prompt(); sc.Scan(); prompt() {
		fields := strings.Fields(sc.Text())
		if len(fields) == 0 {
			continue
		}
		cmd := strings.ToLower(fields[0])
		switch {
		case cmd == "quit" || cmd == "q" || cmd == "exit":
			return
		case cmd == "list" || cmd == "l":
			mu.Lock()
			for _, e := range ems {
				fmt.Printf("  %-24s %s\n", e.key(), e.state)
			}
			mu.Unlock()
		case cmd == "online": // alias: online = up heartbeats again, keep last state? simplest: back to up
			setState(&mu, ems, fields, "up")
		default:
			if _, ok := states[cmd]; !ok {
				fmt.Println("  ? unknown command:", cmd)
				continue
			}
			setState(&mu, ems, fields, cmd)
		}
	}
}

func prompt() { fmt.Print("> ") }

func setState(mu *sync.Mutex, ems []*em, fields []string, state string) {
	if len(fields) < 2 {
		fmt.Println("  ? need an EM pattern, e.g.:", fields[0], "MAG01")
		return
	}
	pat := fields[1]
	reason := strings.Join(fields[2:], " ")
	mu.Lock()
	defer mu.Unlock()
	n := 0
	for _, e := range ems {
		if !matches(pat, e) {
			continue
		}
		e.state = state
		e.alarm = reason
		n++
		fmt.Printf("  %s -> %s%s\n", e.key(), state,
			map[bool]string{true: " (" + reason + ")", false: ""}[reason != ""])
	}
	if n == 0 {
		fmt.Println("  ? no EM matches", pat)
	}
}

func matches(pat string, e *em) bool {
	p := strings.ToLower(pat)
	if strings.Contains(p, "/") {
		ok, _ := path.Match(p, strings.ToLower(e.key()))
		return ok
	}
	ok, _ := path.Match(p, strings.ToLower(e.label))
	return ok
}

// datagram builds a wire-v4 telemetry payload (same layout the FB serializes).
func datagram(bits uint16, seq uint32, station, label, alarm, line string) []byte {
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
	head := make([]byte, 24)
	head[0] = wire.Version
	head[1] = wire.MsgEvent
	binary.BigEndian.PutUint16(head[2:], bits)
	binary.BigEndian.PutUint16(head[4:], 0)
	binary.BigEndian.PutUint16(head[6:], 1) // activeSequence
	binary.BigEndian.PutUint32(head[8:], seq)
	binary.BigEndian.PutUint32(head[12:], 5000)
	binary.BigEndian.PutUint64(head[16:], uint64(time.Now().UnixNano()))
	b := append([]byte{}, head...)
	b = append(b, s7s(station, 32)...)
	b = append(b, s7s(label, 16)...)
	b = append(b, s7s("20", 60)...)   // step
	b = append(b, s7s("Run", 200)...) // step description
	b = append(b, s7s(alarm, 200)...)
	b = append(b, s7s("", 160)...) // interlock fails
	b = append(b, s7s("", 200)...) // fault conditions
	b = append(b, s7s("", 200)...) // waiting on
	b = append(b, s7s(line, 32)...)
	return b
}
