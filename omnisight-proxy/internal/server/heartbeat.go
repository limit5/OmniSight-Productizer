package server

import (
	"bytes"
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"time"

	"github.com/omnisight/productizer/omnisight-proxy/internal/config"
)

type heartbeatPayload struct {
	ProxyID           string `json:"proxy_id"`
	TenantID          string `json:"tenant_id,omitempty"`
	Status            string `json:"status"`
	Service           string `json:"service"`
	ProviderCount     int    `json:"provider_count"`
	HeartbeatInterval int    `json:"heartbeat_interval_seconds"`
}

type heartbeatClient struct {
	url     string
	client  *http.Client
	payload heartbeatPayload
}

func newHeartbeatClient(cfg *config.Settings) *heartbeatClient {
	providerCount := 0
	if cfg.ProviderCatalog != nil {
		providerCount = len(cfg.ProviderCatalog.Providers)
	}
	return &heartbeatClient{
		url: cfg.SaaSHeartbeatURL,
		client: &http.Client{
			Timeout: 5 * time.Second,
		},
		payload: heartbeatPayload{
			ProxyID:           cfg.ProxyID,
			TenantID:          cfg.TenantID,
			Status:            "ok",
			Service:           "omnisight-proxy",
			ProviderCount:     providerCount,
			HeartbeatInterval: cfg.HeartbeatIntervalSeconds,
		},
	}
}

func (c *heartbeatClient) post(ctx context.Context) error {
	body, err := json.Marshal(c.payload)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.url, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < http.StatusOK || resp.StatusCode >= http.StatusMultipleChoices {
		return &heartbeatStatusError{status: resp.StatusCode}
	}
	return nil
}

type heartbeatStatusError struct {
	status int
}

func (e *heartbeatStatusError) Error() string {
	return "heartbeat post returned HTTP status " + http.StatusText(e.status)
}

func StartHeartbeatLoop(ctx context.Context, cfg *config.Settings, logger *slog.Logger) {
	if cfg.SaaSHeartbeatURL == "" {
		return
	}
	client := newHeartbeatClient(cfg)
	interval := time.Duration(cfg.HeartbeatIntervalSeconds) * time.Second
	ticker := time.NewTicker(interval)
	go func() {
		defer ticker.Stop()
		postHeartbeat(ctx, client, logger)
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				postHeartbeat(ctx, client, logger)
			}
		}
	}()
}

func postHeartbeat(ctx context.Context, client *heartbeatClient, logger *slog.Logger) {
	if err := client.post(ctx); err != nil {
		logger.Warn("proxy heartbeat failed", "err", err)
	}
}
