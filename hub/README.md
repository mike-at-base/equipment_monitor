# emhub — equipment-monitor v2 (Phase 1: ingest)

Go ingest service per docs/REDESIGN.md. Receives EquipmentModuleTelemetry
datagrams (wire v3), derives state intervals / step events / cycles / mode
windows / operator events at ingest, and batch-writes them to TimescaleDB
(append-only, never blocks the ingest path).

    cd hub
    go test ./...
    go build -o emhub.exe ./cmd/emhub
    EMHUB_DSN=postgres://monitor:monitor@localhost:5432/emhub \
      ./emhub.exe -config ../config.yaml

Phase 2 (query surface — the math lives here, UI/agents/CLI all read it):

    GET /healthz
    GET /api/v2/live                                    current EM states
    GET /api/v2/lines                                   hierarchy + live rollup
    GET /api/v2/lines/{line}/summary?window=today|8h    availability, states,
                                                        cycles, reasons, MTTR
    GET /api/v2/compare?a=LINE&b=LINE&window=...        decomposed line delta
    GET /api/v2/ems/{line}/{station}/{label}/intervals|steps|cycles|downs

MCP server (same binary, streamable HTTP) at /mcp — connect agents with:

    claude mcp add --transport http emhub http://localhost:8062/mcp

CLI:

    go build -o emctl.exe ./cmd/emctl
    emctl summary SIM1 --window 8h
    emctl compare CELL1 CELL2 --window today
    emctl downs SIM1 ST90000 --window 24h

Phase 3 adds the React SCADA frontend per docs/REDESIGN.md.

The tracker is a port of the state logic proven in the Python collector;
internal/tracker/tracker_test.go replays the same golden scenarios that
validated v1 end-to-end.
