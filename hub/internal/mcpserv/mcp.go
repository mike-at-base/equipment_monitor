// Package mcpserv exposes emhub's query API as MCP tools over the
// streamable-HTTP transport (single JSON responses — no SSE needed for
// request/response tools). Mount at /mcp; agents connect with:
//
//	claude mcp add --transport http emhub http://<host>:8062/mcp
//
// Every tool is a thin wrapper over the REST endpoints, so agents and the
// UI read identical numbers.
package mcpserv

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
)

const protocolVersion = "2025-06-18"

type tool struct {
	Name        string          `json:"name"`
	Description string          `json:"description"`
	InputSchema json.RawMessage `json:"inputSchema"`
	// path builds the internal REST request for the tool call
	path func(args map[string]any) string
}

func windowProp() string {
	return `"window":{"type":"string","description":"today (default), or a duration like 8h, 30m, 3d"}`
}

func str(v any) string {
	s, _ := v.(string)
	return s
}

func win(args map[string]any) string {
	if w := str(args["window"]); w != "" {
		return "window=" + url.QueryEscape(w)
	}
	return "window=today"
}

func tools() []tool {
	return []tool{
		{
			Name:        "list_lines",
			Description: "List production lines with EM counts and a live rollup of current states (productive/starved/blocked/down/...). Start here to discover line names.",
			InputSchema: json.RawMessage(`{"type":"object","properties":{}}`),
			path:        func(map[string]any) string { return "/api/v2/lines" },
		},
		{
			Name:        "live_status",
			Description: "Current state of every equipment module right now: state, reason, current step, how long it has been in that state.",
			InputSchema: json.RawMessage(`{"type":"object","properties":{}}`),
			path:        func(map[string]any) string { return "/api/v2/live" },
		},
		{
			Name: "line_summary",
			Description: "Full performance summary of one line over a window: availability %, minutes per state, cycle statistics (count/p50/p90/work-vs-exchange split), top down reasons, flow losses (starved/blocked with the failing-permissive reasons), mode context (dry cycle etc.), and MTTR split into response vs repair time. Per-EM breakdown included.",
			InputSchema: json.RawMessage(`{"type":"object","required":["line"],"properties":{"line":{"type":"string","description":"line name, e.g. CELL1"},` + windowProp() + `}}`),
			path: func(a map[string]any) string {
				return "/api/v2/lines/" + url.PathEscape(str(a["line"])) + "/summary?" + win(a)
			},
		},
		{
			Name: "compare_lines",
			Description: "Decomposed comparison of two lines over the same window — THE tool for questions like 'why is line A running better than line B'. Returns both full summaries plus a delta (availability, cycles, cycle p50, down/starved/blocked minutes). Cite the reason texts and per-station flow losses when explaining the difference.",
			InputSchema: json.RawMessage(`{"type":"object","required":["a","b"],"properties":{"a":{"type":"string"},"b":{"type":"string"},` + windowProp() + `}}`),
			path: func(args map[string]any) string {
				return "/api/v2/compare?a=" + url.QueryEscape(str(args["a"])) +
					"&b=" + url.QueryEscape(str(args["b"])) + "&" + win(args)
			},
		},
		{
			Name:        "em_downs",
			Description: "Down events for one equipment module with scan-accurate reasons (failing permissive conditions / interlocks / alarm text), ack timestamps, and a reason pareto.",
			InputSchema: emSchema(),
			path: func(a map[string]any) string {
				return emPath(a, "downs") + "?" + win(a)
			},
		},
		{
			Name:        "em_cycles",
			Description: "Cycle records and statistics (p50/p90, work vs exchange phase split, interrupted count) for one equipment module.",
			InputSchema: emSchema(),
			path: func(a map[string]any) string {
				return emPath(a, "cycles") + "?" + win(a)
			},
		},
		{
			Name:        "em_steps",
			Description: "Step-by-step execution history for one equipment module (step name, description, duration, faulted flag).",
			InputSchema: emSchema(),
			path: func(a map[string]any) string {
				return emPath(a, "steps") + "?" + win(a) + "&limit=500"
			},
		},
		{
			Name:        "em_intervals",
			Description: "Raw state timeline for one equipment module: every state interval (productive/standby/down/starved/blocked/paused/manual/offline) with reasons.",
			InputSchema: emSchema(),
			path: func(a map[string]any) string {
				return emPath(a, "intervals") + "?" + win(a)
			},
		},
	}
}

func emSchema() json.RawMessage {
	return json.RawMessage(`{"type":"object","required":["line","station"],"properties":{"line":{"type":"string"},"station":{"type":"string","description":"e.g. ST90000"},"em_label":{"type":"string","description":"default: main"},` + windowProp() + `}}`)
}

func emPath(a map[string]any, leaf string) string {
	label := str(a["em_label"])
	if label == "" {
		label = "main"
	}
	return "/api/v2/ems/" + url.PathEscape(str(a["line"])) + "/" +
		url.PathEscape(str(a["station"])) + "/" + url.PathEscape(label) + "/" + leaf
}

// ── JSON-RPC plumbing ────────────────────────────────────────────────────

type rpcReq struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params"`
}

type rpcResp struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id"`
	Result  any             `json:"result,omitempty"`
	Error   *rpcError       `json:"error,omitempty"`
}

type rpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

// Handler serves MCP over streamable HTTP by dispatching tool calls into
// the same mux that serves the REST API (in-process, no network hop).
func Handler(apiMux *http.ServeMux) http.HandlerFunc {
	reg := tools()
	byName := map[string]tool{}
	for _, t := range reg {
		byName[t.Name] = t
	}

	callREST := func(path string) (string, int) {
		req := httptest.NewRequest("GET", path, nil)
		rec := httptest.NewRecorder()
		apiMux.ServeHTTP(rec, req)
		body, _ := io.ReadAll(rec.Result().Body)
		return string(body), rec.Code
	}

	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		var req rpcReq
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		// notifications get 202 and no body
		if req.ID == nil {
			w.WriteHeader(http.StatusAccepted)
			return
		}
		resp := rpcResp{JSONRPC: "2.0", ID: req.ID}
		switch req.Method {
		case "initialize":
			resp.Result = map[string]any{
				"protocolVersion": protocolVersion,
				"capabilities":    map[string]any{"tools": map[string]any{}},
				"serverInfo": map[string]any{
					"name": "emhub", "version": "2.0.0",
				},
			}
		case "ping":
			resp.Result = map[string]any{}
		case "tools/list":
			list := make([]map[string]any, 0, len(reg))
			for _, t := range reg {
				list = append(list, map[string]any{
					"name": t.Name, "description": t.Description,
					"inputSchema": t.InputSchema,
				})
			}
			resp.Result = map[string]any{"tools": list}
		case "tools/call":
			var p struct {
				Name string         `json:"name"`
				Args map[string]any `json:"arguments"`
			}
			if err := json.Unmarshal(req.Params, &p); err != nil {
				resp.Error = &rpcError{Code: -32602, Message: err.Error()}
				break
			}
			t, ok := byName[p.Name]
			if !ok {
				resp.Error = &rpcError{Code: -32602, Message: "unknown tool " + p.Name}
				break
			}
			body, code := callREST(t.path(p.Args))
			isErr := code >= 400
			resp.Result = map[string]any{
				"content": []map[string]any{{"type": "text", "text": body}},
				"isError": isErr,
			}
		default:
			resp.Error = &rpcError{Code: -32601, Message: fmt.Sprintf("method %q not supported", req.Method)}
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(resp)
	}
}
