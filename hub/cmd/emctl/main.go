// emctl — CLI for the emhub query API.
//
//	emctl live [LINE]
//	emctl lines
//	emctl summary LINE [--window 8h]
//	emctl compare A B [--window today]
//	emctl downs LINE STATION [LABEL] [--window 24h]
//	emctl cycles LINE STATION [LABEL] [--window 8h]
//
// Flags: --window today|8h|3d   --json (raw API output)
// Env:   EMHUB_URL (default http://localhost:8062)
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"sort"
	"strings"
	"text/tabwriter"
)

var base = func() string {
	if v := os.Getenv("EMHUB_URL"); v != "" {
		return strings.TrimRight(v, "/")
	}
	return "http://localhost:8062"
}()

func get(path string) ([]byte, error) {
	resp, err := http.Get(base + path)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return nil, fmt.Errorf("%s: %s", resp.Status, strings.TrimSpace(string(body)))
	}
	return body, nil
}

func main() {
	args := []string{}
	window, rawJSON := "today", false
	for i := 1; i < len(os.Args); i++ {
		switch a := os.Args[i]; {
		case a == "--json":
			rawJSON = true
		case a == "--window":
			i++
			if i < len(os.Args) {
				window = os.Args[i]
			}
		case strings.HasPrefix(a, "--window="):
			window = strings.TrimPrefix(a, "--window=")
		default:
			args = append(args, a)
		}
	}
	if len(args) == 0 {
		usage()
	}

	var path string
	cmd := args[0]
	switch cmd {
	case "lines":
		path = "/api/v2/lines"
	case "live":
		path = "/api/v2/live"
	case "summary":
		need(args, 2)
		path = "/api/v2/lines/" + url.PathEscape(args[1]) + "/summary?window=" + url.QueryEscape(window)
	case "compare":
		need(args, 3)
		path = "/api/v2/compare?a=" + url.QueryEscape(args[1]) +
			"&b=" + url.QueryEscape(args[2]) + "&window=" + url.QueryEscape(window)
	case "downs", "cycles", "steps", "intervals":
		need(args, 3)
		label := "main"
		if len(args) > 3 {
			label = args[3]
		}
		path = "/api/v2/ems/" + url.PathEscape(args[1]) + "/" + url.PathEscape(args[2]) +
			"/" + url.PathEscape(label) + "/" + cmd + "?window=" + url.QueryEscape(window)
	default:
		usage()
	}

	body, err := get(path)
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
	if rawJSON {
		fmt.Println(string(body))
		return
	}
	pretty(cmd, args, body)
}

func need(args []string, n int) {
	if len(args) < n {
		usage()
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, `usage:
  emctl lines
  emctl live
  emctl summary LINE            [--window today|8h|3d] [--json]
  emctl compare LINE_A LINE_B   [--window ...]
  emctl downs   LINE STATION [LABEL] [--window ...]
  emctl cycles  LINE STATION [LABEL] [--window ...]
  emctl steps   LINE STATION [LABEL] [--window ...]
  emctl intervals LINE STATION [LABEL] [--window ...]`)
	os.Exit(2)
}

// ── human output ─────────────────────────────────────────────────────────

func pretty(cmd string, args []string, body []byte) {
	w := tabwriter.NewWriter(os.Stdout, 2, 4, 2, ' ', 0)
	defer w.Flush()
	switch cmd {
	case "live":
		var ems []map[string]any
		_ = json.Unmarshal(body, &ems)
		sort.Slice(ems, func(i, j int) bool {
			a := fmt.Sprint(ems[i]["line"], ems[i]["station"], ems[i]["em_label"])
			b := fmt.Sprint(ems[j]["line"], ems[j]["station"], ems[j]["em_label"])
			return a < b
		})
		fmt.Fprintln(w, "LINE\tSTATION\tEM\tSTATE\tSTEP\tREASON")
		for _, e := range ems {
			if e["state"] == "" {
				continue
			}
			fmt.Fprintf(w, "%v\t%v\t%v\t%v\t%v\t%v\n", e["line"], e["station"],
				e["em_label"], e["state"], s(e["step"]), trunc(s(e["reason"]), 60))
		}
	case "summary":
		var v struct {
			Line            string             `json:"line"`
			AvailabilityPct *float64           `json:"availability_pct"`
			StateMin        map[string]float64 `json:"state_min"`
			Cycles          map[string]any     `json:"cycles"`
			TopDownReasons  []map[string]any   `json:"top_down_reasons"`
			FlowLosses      []map[string]any   `json:"flow_losses"`
			MTTR            map[string]any     `json:"mttr"`
		}
		_ = json.Unmarshal(body, &v)
		fmt.Fprintf(w, "line\t%s\n", v.Line)
		if v.AvailabilityPct != nil {
			fmt.Fprintf(w, "availability\t%.1f%%\n", *v.AvailabilityPct)
		}
		for _, st := range sortedKeys(v.StateMin) {
			fmt.Fprintf(w, "%s\t%.1f min\n", st, v.StateMin[st])
		}
		fmt.Fprintf(w, "cycles\t%v (p50 %v ms, %v/h, %v interrupted)\n",
			v.Cycles["count"], v.Cycles["p50_ms"], v.Cycles["per_hour"], v.Cycles["interrupted"])
		for i, tr := range v.TopDownReasons {
			fmt.Fprintf(w, "down #%d\t%v min x%v  %v\n", i+1, tr["minutes"], tr["count"],
				trunc(s(tr["reason"]), 70))
		}
		for _, fl := range v.FlowLosses {
			fmt.Fprintf(w, "%v %v\t%v min  (%v)\n", fl["station"], fl["state"],
				fl["minutes"], trunc(s(fl["top_reason"]), 50))
		}
	case "compare":
		var v struct {
			A     json.RawMessage `json:"a"`
			B     json.RawMessage `json:"b"`
			Delta map[string]any  `json:"delta_a_minus_b"`
		}
		_ = json.Unmarshal(body, &v)
		var a, b struct {
			Line            string   `json:"line"`
			AvailabilityPct *float64 `json:"availability_pct"`
			Cycles          struct {
				Count   int      `json:"count"`
				P50     *float64 `json:"p50_ms"`
				PerHour *float64 `json:"per_hour"`
			} `json:"cycles"`
			StateMin map[string]float64 `json:"state_min"`
		}
		_ = json.Unmarshal(v.A, &a)
		_ = json.Unmarshal(v.B, &b)
		fmt.Fprintf(w, "\t%s\t%s\tdelta\n", a.Line, b.Line)
		fmt.Fprintf(w, "availability\t%v\t%v\t%v\n", pct(a.AvailabilityPct), pct(b.AvailabilityPct), v.Delta["availability_pct"])
		fmt.Fprintf(w, "cycles\t%d\t%d\t%v\n", a.Cycles.Count, b.Cycles.Count, v.Delta["cycles"])
		fmt.Fprintf(w, "cycle p50 ms\t%v\t%v\t%v\n", f(a.Cycles.P50), f(b.Cycles.P50), v.Delta["cycle_p50_ms"])
		for _, st := range []string{"down", "starved", "blocked", "productive"} {
			fmt.Fprintf(w, "%s min\t%.1f\t%.1f\t\n", st, a.StateMin[st], b.StateMin[st])
		}
	default:
		// fallback: pretty-printed JSON
		var buf any
		_ = json.Unmarshal(body, &buf)
		out, _ := json.MarshalIndent(buf, "", "  ")
		fmt.Println(string(out))
	}
}

func s(v any) string {
	if v == nil {
		return ""
	}
	return fmt.Sprint(v)
}

func f(v *float64) string {
	if v == nil {
		return "-"
	}
	return fmt.Sprintf("%.0f", *v)
}

func pct(v *float64) string {
	if v == nil {
		return "-"
	}
	return fmt.Sprintf("%.1f%%", *v)
}

func trunc(s string, n int) string {
	if len(s) > n {
		return s[:n-1] + "…"
	}
	return s
}

func sortedKeys(m map[string]float64) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
