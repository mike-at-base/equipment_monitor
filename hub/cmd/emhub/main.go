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
	emIDs, err := pg.SyncConfig(ctx, lineCfgs)
	if err != nil {
		log.Error("config sync", "err", err)
		os.Exit(1)
	}

	// build trackers keyed by (source ip, station, em label)
	trackers := map[ingest.Key]*tracker.EM{}
	lineByHost := map[string]string{}
	total := 0
	for _, l := range cfg.Lines {
		if !l.IsEnabled() || l.Host() == "" {
			continue
		}
		lineByHost[l.Host()] = l.Name
		for _, e := range l.EMs {
			if !e.IsEnabled() {
				continue
			}
			key := ingest.Key{Host: l.Host(),
				Station: lowerASCII(e.Station), EMLabel: lowerASCII(e.EMLabel)}
			emID := emIDs[l.Name][[2]string{lowerASCII(e.Station), lowerASCII(e.EMLabel)}]
			seqs := map[int16]tracker.SeqConfig{}
			for _, s := range e.Sequences {
				seqs[s.Index] = tracker.SeqConfig{
					Index: s.Index, IsProduction: s.IsProduction,
					CycleStart: s.CycleStart, CycleComplete: s.CycleComplete,
				}
			}
			trackers[key] = tracker.New(tracker.Config{
				EMID: emID, Station: e.Station, EMLabel: e.EMLabel, Sequences: seqs,
			}, pg)
			total++
		}
	}
	log.Info("configured", "lines", len(lineCfgs), "ems", total)

	go pg.Run(ctx)

	svc := ingest.New(cfg.Telemetry.ListenPort, trackers, log)

	// query API hierarchy (only enabled lines/EMs, with db ids)
	var apiLines []api.LineInfo
	for _, l := range cfg.Lines {
		if !l.IsEnabled() {
			continue
		}
		li := api.LineInfo{Name: l.Name}
		for _, e := range l.EMs {
			if !e.IsEnabled() {
				continue
			}
			id := emIDs[l.Name][[2]string{lowerASCII(e.Station), lowerASCII(e.EMLabel)}]
			li.EMs = append(li.EMs, api.EMInfo{ID: id, Station: e.Station,
				Label: e.EMLabel, Display: e.DisplayName})
		}
		apiLines = append(apiLines, li)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})
	mux.HandleFunc("GET /api/v2/live", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(svc.Snapshot(lineByHost))
	})
	apiSrv := api.New(pg.Pool(), apiLines, func() []ingest.LiveEM {
		return svc.Snapshot(lineByHost)
	})
	apiSrv.Register(mux)
	mux.HandleFunc("/mcp", mcpserv.Handler(mux))
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
