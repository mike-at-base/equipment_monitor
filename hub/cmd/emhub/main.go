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
	"flag"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"time"

	"github.com/mike-at-base/equipment_monitor/hub/internal/api"
	"github.com/mike-at-base/equipment_monitor/hub/internal/config"
	"github.com/mike-at-base/equipment_monitor/hub/internal/ingest"
	"github.com/mike-at-base/equipment_monitor/hub/internal/mcpserv"
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
	cfgPath := flag.String("config", "config.yaml", "path to config.yaml")
	flag.Parse()
	log := slog.New(slog.NewTextHandler(os.Stdout, nil))

	cfg, err := config.Load(*cfgPath)
	if err != nil {
		log.Error("config", "err", err)
		os.Exit(1)
	}

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

	// sync config -> hierarchy tables, get em ids
	var lineCfgs []store.LineConfig
	for _, l := range cfg.Lines {
		lc := store.LineConfig{Name: l.Name, Host: l.Host(), Enabled: l.IsEnabled()}
		for _, e := range l.EMs {
			ec := store.EMConfig{Station: e.Station, EMLabel: e.EMLabel,
				DisplayName: e.DisplayName, Enabled: e.IsEnabled()}
			for _, s := range e.Sequences {
				ec.Sequences = append(ec.Sequences, store.SeqConfig{
					Index: s.Index, Name: s.Name, IsProduction: s.IsProduction,
					CycleStart: s.CycleStart, CycleComplete: s.CycleComplete,
				})
			}
			lc.EMs = append(lc.EMs, ec)
		}
		lineCfgs = append(lineCfgs, lc)
	}
	// config.yaml is a one-time SEED now — SyncConfig inserts what's missing
	// but never overwrites DB rows (UI edits win). The DB is the source of
	// truth the collector and API build from.
	if err := pg.SyncConfig(ctx, lineCfgs); err != nil {
		log.Error("config sync", "err", err)
		os.Exit(1)
	}

	// lineByHost supports the legacy v3 path (route by source IP); v4 datagrams
	// self-identify their line and don't need it.
	lineByHost := map[string]string{}
	for _, l := range cfg.Lines {
		if l.IsEnabled() && l.Host() != "" {
			lineByHost[l.Host()] = l.Name
		}
	}

	// build trackers + the query-API hierarchy from the DB (seed + any
	// previously auto-discovered EMs survive restarts this way).
	recs, err := pg.LoadHierarchy(ctx)
	if err != nil {
		log.Error("load hierarchy", "err", err)
		os.Exit(1)
	}
	trackers := map[ingest.Key]*tracker.EM{}
	total := 0
	for _, l := range recs {
		for _, e := range l.EMs {
			key := ingest.Key{Line: lowerASCII(l.Name),
				Station: lowerASCII(e.Station), EMLabel: lowerASCII(e.EMLabel)}
			seqs := map[int16]tracker.SeqConfig{}
			for _, s := range e.Sequences {
				seqs[s.Index] = tracker.SeqConfig{
					Index: s.Index, IsProduction: s.IsProduction,
					CycleStart: s.CycleStart, CycleComplete: s.CycleComplete,
				}
			}
			trackers[key] = tracker.New(tracker.Config{
				EMID: e.ID, Line: l.Name, Station: e.Station, EMLabel: e.EMLabel, Sequences: seqs,
			}, pg)
			total++
		}
	}
	log.Info("loaded hierarchy", "lines", len(recs), "ems", total)

	go pg.Run(ctx)

	// discoverer: auto-register a v4 EM seen for the first time (unconfirmed).
	discover := func(line, station, emLabel string, wireVer int) (*tracker.EM, error) {
		emID, err := pg.RegisterEM(ctx, line, station, emLabel, wireVer)
		if err != nil {
			return nil, err
		}
		return tracker.New(tracker.Config{EMID: emID, Line: line, Station: station,
			EMLabel: emLabel, Sequences: map[int16]tracker.SeqConfig{}}, pg), nil
	}

	svc := ingest.New(cfg.Telemetry.ListenPort, trackers, lineByHost, discover, log)

	apiLines := buildAPILines(recs)

	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})
	mux.HandleFunc("GET /api/v2/live", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(svc.Snapshot())
	})
	apiSrv := api.New(pg.Pool(), apiLines, func() []ingest.LiveEM {
		return svc.Snapshot()
	}, func() []ingest.RawEM {
		return svc.RawSnapshot()
	})
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

// buildAPILines projects the DB hierarchy into the query-API shape.
func buildAPILines(recs []store.LineRec) []api.LineInfo {
	out := make([]api.LineInfo, 0, len(recs))
	for _, l := range recs {
		li := api.LineInfo{Name: l.Name}
		for _, e := range l.EMs {
			li.EMs = append(li.EMs, api.EMInfo{ID: e.ID, Station: e.Station,
				Label: e.EMLabel, Display: e.DisplayName})
		}
		out = append(out, li)
	}
	return out
}
