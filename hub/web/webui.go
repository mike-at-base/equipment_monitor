// Package web embeds the built SPA (dist/) into the emhub binary so the
// whole SCADA ships as one executable. Run `npm run build` in hub/web
// before `go build`.
package web

import (
	"embed"
	"io/fs"
	"net/http"
	"strings"
)

//go:embed all:dist
var dist embed.FS

// Handler serves the SPA with history-API fallback: unknown paths (that
// aren't real files) get index.html so client-side routes deep-link.
func Handler() http.Handler {
	sub, err := fs.Sub(dist, "dist")
	if err != nil {
		panic(err)
	}
	files := http.FS(sub)
	fileServer := http.FileServer(files)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path := strings.TrimPrefix(r.URL.Path, "/")
		if path != "" {
			if f, err := files.Open("/" + path); err == nil {
				f.Close()
				fileServer.ServeHTTP(w, r)
				return
			}
		}
		// SPA fallback
		r.URL.Path = "/"
		fileServer.ServeHTTP(w, r)
	})
}
