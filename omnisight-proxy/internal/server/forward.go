package server

import (
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"

	"github.com/omnisight/productizer/omnisight-proxy/internal/auth"
	"github.com/omnisight/productizer/omnisight-proxy/internal/config"
)

const llmForwardPrefix = "/v1/llm/"

type llmForwarder struct {
	catalog *config.ProviderCatalog
	client  *http.Client
}

func newLLMForwarder(cfg *config.Settings) http.Handler {
	return &llmForwarder{
		catalog: cfg.ProviderCatalog,
		client:  &http.Client{},
	}
}

func (f *llmForwarder) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	providerName, upstreamPath, ok := strings.Cut(strings.TrimPrefix(r.URL.Path, llmForwardPrefix), "/")
	if !ok || providerName == "" || upstreamPath == "" {
		http.Error(w, "provider and upstream path are required", http.StatusBadRequest)
		return
	}
	if f.catalog == nil {
		http.Error(w, "provider catalog is not configured", http.StatusServiceUnavailable)
		return
	}
	provider, ok := f.catalog.FindProvider(providerName)
	if !ok {
		http.Error(w, "provider not configured", http.StatusNotFound)
		return
	}
	targetURL, err := forwardURL(provider.BaseURL, upstreamPath, r.URL.RawQuery)
	if err != nil {
		http.Error(w, err.Error(), http.StatusServiceUnavailable)
		return
	}
	providerKey, err := provider.KeySource.ReadLocalFileKey()
	if err != nil {
		http.Error(w, err.Error(), http.StatusServiceUnavailable)
		return
	}

	outbound, err := http.NewRequestWithContext(r.Context(), r.Method, targetURL, r.Body)
	if err != nil {
		http.Error(w, "build upstream request failed", http.StatusInternalServerError)
		return
	}
	copyRequestHeaders(outbound.Header, r.Header)
	for key, value := range provider.Headers {
		outbound.Header.Set(key, value)
	}
	outbound.Header.Set("Authorization", "Bearer "+providerKey)

	response, err := f.client.Do(outbound)
	if err != nil {
		http.Error(w, "upstream request failed: "+err.Error(), http.StatusBadGateway)
		return
	}
	defer response.Body.Close()

	copyResponseHeaders(w.Header(), response.Header)
	w.WriteHeader(response.StatusCode)
	if flusher, ok := w.(http.Flusher); ok {
		if _, err := io.Copy(flushWriter{writer: w, flusher: flusher}, response.Body); err != nil {
			return
		}
		return
	}
	if _, err := io.Copy(w, response.Body); err != nil {
		return
	}
}

type flushWriter struct {
	writer  io.Writer
	flusher http.Flusher
}

func (w flushWriter) Write(p []byte) (int, error) {
	n, err := w.writer.Write(p)
	w.flusher.Flush()
	return n, err
}

func forwardURL(baseURL string, upstreamPath string, rawQuery string) (string, error) {
	baseURL = strings.TrimSpace(baseURL)
	if baseURL == "" {
		return "", fmt.Errorf("provider base_url is required for forwarding")
	}
	parsed, err := url.Parse(baseURL)
	if err != nil || parsed.Host == "" {
		return "", fmt.Errorf("provider base_url must be an absolute URL")
	}
	if parsed.Scheme != "http" && parsed.Scheme != "https" {
		return "", fmt.Errorf("provider base_url must use http or https")
	}
	parsed.Path = joinURLPath(parsed.Path, upstreamPath)
	parsed.RawQuery = rawQuery
	return parsed.String(), nil
}

func joinURLPath(basePath string, upstreamPath string) string {
	basePath = strings.TrimRight(basePath, "/")
	upstreamPath = strings.TrimLeft(upstreamPath, "/")
	if basePath == "" {
		return "/" + upstreamPath
	}
	return basePath + "/" + upstreamPath
}

func copyRequestHeaders(dst http.Header, src http.Header) {
	for key, values := range src {
		if shouldSkipHeader(key) || strings.HasPrefix(http.CanonicalHeaderKey(key), "X-Omnisight-") {
			continue
		}
		for _, value := range values {
			dst.Add(key, value)
		}
	}
}

func copyResponseHeaders(dst http.Header, src http.Header) {
	for key, values := range src {
		if shouldSkipHeader(key) {
			continue
		}
		for _, value := range values {
			dst.Add(key, value)
		}
	}
}

func shouldSkipHeader(key string) bool {
	switch http.CanonicalHeaderKey(key) {
	case "Connection", "Keep-Alive", "Proxy-Authenticate", "Proxy-Authorization",
		"Te", "Trailer", "Transfer-Encoding", "Upgrade",
		auth.HeaderSignature, auth.HeaderNonce, auth.HeaderTimestamp, auth.HeaderTenantID:
		return true
	default:
		return false
	}
}
