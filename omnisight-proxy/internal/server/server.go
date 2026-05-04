// Package server owns the HTTP surface for the KS.3.1 container smoke path.

package server

import (
	"encoding/json"
	"net/http"

	"github.com/omnisight/productizer/omnisight-proxy/internal/config"
)

type healthResponse struct {
	Status  string `json:"status"`
	Service string `json:"service"`
}

// NewHandler returns the proxy HTTP handler. KS.3.1 intentionally exposes
// only a local smoke endpoint; forwarding, streaming, and auth belong to
// later KS.3 rows.
func NewHandler(_ *config.Settings) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", healthz)
	return mux
}

func healthz(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.Header().Set("Allow", http.MethodGet)
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_ = json.NewEncoder(w).Encode(healthResponse{
		Status:  "ok",
		Service: "omnisight-proxy",
	})
}
