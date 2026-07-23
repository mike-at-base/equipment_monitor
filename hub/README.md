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

Endpoints (Phase 1): GET /healthz, GET /api/v2/live (current EM states).
Phase 2 adds the full query API + MCP + emctl per the redesign doc.

The tracker is a port of the state logic proven in the Python collector;
internal/tracker/tracker_test.go replays the same golden scenarios that
validated v1 end-to-end.
