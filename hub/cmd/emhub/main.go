// emhub — equipment-monitor v2 ingest service (Phase 1).
//
//	emhub -config ../config.yaml
//
// Env:
//	EMHUB_DSN   postgres DSN (default local dev TimescaleDB, db "emhub")
//	EMHUB_HTTP  http listen address for /healthz + /api/v2/live (default :8060)
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"time"

	"github.com/mike-at-base/equipment_monitor/hub/internal/api"
	"github.com/mike-at-base/equipment_monitor/hub/internal/ingest"
	"github.com/mike-at-base/equipment_monitor/hub/internal/mcpserv"
	"github.com/mike-at-base/equipment_monitor/hub/internal/simulator"
	"github.com/mike-at-base/equipment_monitor/hub/internal/store"
	"github.com/mike-at-base/equipment_monitor/hub/internal/tracker"
	"github.com/mike-at-base/equipment_monitor/hub/web"
)

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func main() {
	log := slog.New(slog.NewTextHandler(os.Stdout, nil))

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()

	dsn := env("EMHUB_DSN",
		"postgres://monitor:monitor@localhost:5432/emhub")
	pg, err := store.Open(ctx, dsn, log)
	if err != nil {
		log.Error("store", "err", err)
		os.Exit(1)
	}
	defer pg.Close()
	if err := pg.InitSchema(ctx); err != nil {
		log.Error("schema", "err", err)
		os.Exit(1)
	}

	// The DB is the sole source of truth: EMs auto-discover from v4 telemetry
	// and are configured/confirmed in the UI. Build the trackers + query-API
	// hierarchy from it (auto-discovered EMs survive restarts).
	recs, err := pg.LoadHierarchy(ctx)
	if err != nil {
		log.Error("load hierarchy", "err", err)
		os.Exit(1)
	}
	trackers := map[ingest.Key]*tracker.EM{}
	total := 0
	for _, l := range recs {
		for _, st := range l.Stations {
			for _, e := range st.EMs {
				key := ingest.Key{Line: lowerASCII(l.Name),
					Station: lowerASCII(st.Name), EMLabel: lowerASCII(e.EMLabel)}
				seqs := map[int16]tracker.SeqConfig{}
				for _, s := range e.Sequences {
					seqs[s.Index] = toSeqConfig(s)
				}
				trackers[key] = tracker.New(tracker.Config{
					EMID: e.ID, Line: l.Name, Station: st.Name, EMLabel: e.EMLabel, Sequences: seqs,
				}, pg)
				total++
			}
		}
	}
	log.Info("loaded hierarchy", "lines", len(recs), "ems", total)

	go pg.Run(ctx)

	// discoverer: auto-register a v4 EM (line->station->em) on first sight, unconfirmed.
	discover := func(line, station, emLabel string, wireVer int) (*tracker.EM, error) {
		emID, err := pg.RegisterEM(ctx, line, station, emLabel, wireVer)
		if err != nil {
			return nil, err
		}
		return tracker.New(tracker.Config{EMID: emID, Line: line, Station: station,
			EMLabel: emLabel, Sequences: map[int16]tracker.SeqConfig{}}, pg), nil
	}

	svc := ingest.New(envInt("EMHUB_UDP_PORT", 15020), trackers, discover, log)

	apiLines := buildAPILines(recs)

	// onConfigChange: after a review-&-confirm save, reload the tracker's
	// sequence config from the DB and refresh the API hierarchy so the new
	// name/confirmed state show immediately. apiSrv is captured by reference
	// (assigned just below) so the closure sees it when invoked.
	var apiSrv *api.Server
	onConfigChange := func(emID int) {
		seqs, err := pg.SeqConfigFor(ctx, emID)
		if err != nil {
			log.Warn("reload seq config", "em", emID, "err", err)
			return
		}
		m := map[int16]tracker.SeqConfig{}
		for _, s := range seqs {
			m[s.Index] = toSeqConfig(s)
		}
		svc.SetSequences(emID, m)
		if r, err := pg.LoadHierarchy(ctx); err == nil {
			apiSrv.SetLines(buildAPILines(r))
		}
	}

	// onDelete: dismiss a phantom EM — drop its tracker, delete its rows, and
	// refresh the hierarchy so it disappears from the UI.
	onDelete := func(emID int) {
		svc.RemoveEM(emID)
		if err := pg.DeleteEM(ctx, emID); err != nil {
			log.Warn("delete em", "em", emID, "err", err)
		}
		if r, err := pg.LoadHierarchy(ctx); err == nil {
			apiSrv.SetLines(buildAPILines(r))
		}
	}

	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})
	mux.HandleFunc("GET /api/v2/live", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(svc.Snapshot())
	})
	apiSrv = api.New(pg.Pool(), apiLines, func() []ingest.LiveEM {
		return svc.Snapshot()
	}, func() []ingest.RawEM {
		return svc.RawSnapshot()
	}, onConfigChange, onDelete)
	// built-in UI simulator feeds the hub's own collector over real UDP so
	// simulated telemetry takes the exact same path as a PLC's.
	apiSrv.SetSim(simulator.New(fmt.Sprintf("127.0.0.1:%d", envInt("EMHUB_UDP_PORT", 15020))))
	apiSrv.Register(mux)
	mux.HandleFunc("/mcp", mcpserv.Handler(mux))

	// keep the query-API hierarchy fresh so auto-discovered EMs appear without
	// a restart (DB is the source of truth).
	go func() {
		t := time.NewTicker(5 * time.Second)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				if r, err := pg.LoadHierarchy(ctx); err == nil {
					apiSrv.SetLines(buildAPILines(r))
				}
			}
		}
	}()

	// SSE live stream for the SCADA frontend (1 Hz snapshots)
	mux.HandleFunc("GET /api/v2/stream", func(w http.ResponseWriter, r *http.Request) {
		fl, ok := w.(http.Flusher)
		if !ok {
			http.Error(w, "streaming unsupported", http.StatusInternalServerError)
			return
		}
		w.Header().Set("Content-Type", "text/event-stream")
		w.Header().Set("Cache-Control", "no-cache")
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		send := func() bool {
			payload, err := json.Marshal(svc.Snapshot())
			if err != nil {
				return false
			}
			if _, err := w.Write([]byte("data: ")); err != nil {
				return false
			}
			if _, err := w.Write(payload); err != nil {
				return false
			}
			if _, err := w.Write([]byte("\n\n")); err != nil {
				return false
			}
			fl.Flush()
			return true
		}
		if !send() {
			return
		}
		for {
			select {
			case <-r.Context().Done():
				return
			case <-ticker.C:
				if !send() {
					return
				}
			}
		}
	})

	// SPA (embedded dist) — mounted last so /api, /mcp, /healthz win
	mux.Handle("/", web.Handler())
	httpAddr := env("EMHUB_HTTP", ":8060")
	go func() {
		log.Info("http up", "addr", httpAddr)
		if err := http.ListenAndServe(httpAddr, mux); err != nil {
			log.Error("http", "err", err)
		}
	}()

	if err := svc.Run(ctx); err != nil {
		log.Error("ingest", "err", err)
	}
	svc.FlushAll(time.Now().UTC())
	stop()
	pg.Wait()
	log.Info("emhub stopped")
}

func lowerASCII(s string) string {
	b := []byte(s)
	for i, c := range b {
		if c >= 'A' && c <= 'Z' {
			b[i] = c + 32
		}
	}
	return string(b)
}

// buildAPILines projects the DB hierarchy (line -> station -> em) into the
// query-API shape.
func buildAPILines(recs []store.LineRec) []api.LineInfo {
	out := make([]api.LineInfo, 0, len(recs))
	for _, l := range recs {
		li := api.LineInfo{Name: l.Name, Display: l.DisplayName}
		for _, st := range l.Stations {
			si := api.StationInfo{Name: st.Name, Display: st.DisplayName, PLC: st.PLC}
			for _, e := range st.EMs {
				si.EMs = append(si.EMs, api.EMInfo{ID: e.ID, Station: st.Name,
					Label: e.EMLabel, Display: e.DisplayName, Confirmed: e.Confirmed})
			}
			li.Stations = append(li.Stations, si)
		}
		out = append(out, li)
	}
	return out
}

// toSeqConfig maps a stored sequence config to the tracker's form, turning
// the step lists into lookup sets.
func toSeqConfig(s store.SeqConfig) tracker.SeqConfig {
	toSet := func(xs []string) map[string]bool {
		m := make(map[string]bool, len(xs))
		for _, x := range xs {
			m[x] = true
		}
		return m
	}
	return tracker.SeqConfig{
		Index: s.Index, IsProduction: s.IsProduction,
		CycleStart: s.CycleStart, CycleComplete: s.CycleComplete,
		StarvedSteps: toSet(s.StarvedSteps), BlockedSteps: toSet(s.BlockedSteps),
	}
}

// envInt reads an int env var with a default.
func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}
