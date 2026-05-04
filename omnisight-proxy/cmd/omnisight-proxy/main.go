// KS.3.1 — omnisight-proxy entry point.
//
// This process stays a small Go stdlib binary. KS.3.2 adds the customer
// proxy auth envelope; later KS.3 rows own provider config and request
// forwarding.

package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/omnisight/productizer/omnisight-proxy/internal/auth"
	"github.com/omnisight/productizer/omnisight-proxy/internal/config"
	"github.com/omnisight/productizer/omnisight-proxy/internal/server"
)

func main() {
	cfg, err := config.Load()
	if err != nil {
		_, _ = os.Stderr.WriteString("config load failed: " + err.Error() + "\n")
		os.Exit(2)
	}

	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
		Level: cfg.SlogLevel(),
	}))
	slog.SetDefault(logger)

	if cfg.AuthEnabled {
		if _, err := auth.New(cfg); err != nil {
			logger.Error("proxy auth config failed", "err", err)
			os.Exit(2)
		}
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	srv := &http.Server{
		Addr:              cfg.Addr,
		Handler:           server.NewHandler(cfg),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
	if cfg.AuthEnabled {
		tlsConfig, err := auth.ServerTLSConfig(cfg)
		if err != nil {
			logger.Error("mTLS config failed", "err", err)
			os.Exit(2)
		}
		srv.TLSConfig = tlsConfig
	}

	go func() {
		logger.Info("omnisight-proxy starting", "addr", cfg.Addr)
		if err := listenAndServe(srv, cfg); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Error("listen failed", "err", err)
			stop()
		}
	}()

	<-ctx.Done()
	logger.Info("shutdown signal received, draining connections")

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		logger.Error("graceful shutdown failed", "err", err)
		os.Exit(1)
	}
	logger.Info("omnisight-proxy stopped")
}

func listenAndServe(srv *http.Server, cfg *config.Settings) error {
	if cfg.AuthEnabled {
		return srv.ListenAndServeTLS(cfg.ServerCertFile, cfg.ServerKeyFile)
	}
	return srv.ListenAndServe()
}
