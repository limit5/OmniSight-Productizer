// Package server owns the HTTP surface for the BYOG proxy smoke and auth paths.

package server

import (
	"encoding/json"
	"net/http"

	"github.com/omnisight/productizer/omnisight-proxy/internal/auth"
	"github.com/omnisight/productizer/omnisight-proxy/internal/config"
)

type healthResponse struct {
	Status  string `json:"status"`
	Service string `json:"service"`
}

// NewHandler returns the proxy HTTP handler. KS.3.2 keeps /healthz as the
// local smoke endpoint and pins the protected auth envelope on /auth/verify;
// forwarding and streaming belong to later KS.3 rows.
func NewHandler(cfg *config.Settings) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", healthz)
	var protected http.Handler = http.HandlerFunc(authVerify)
	if cfg.AuthEnabled {
		authenticator, err := auth.New(cfg)
		if err != nil {
			return configErrorHandler(err)
		}
		protected = authenticator.Middleware(protected)
	}
	mux.Handle("/auth/verify", protected)
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

func authVerify(w http.ResponseWriter, r *http.Request) {
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

func configErrorHandler(err error) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "proxy auth config failed: "+err.Error(), http.StatusInternalServerError)
	})
}
