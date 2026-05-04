// Package auth owns the KS.3.2 proxy authentication envelope.

package auth

import (
	"crypto/hmac"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"fmt"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/omnisight/productizer/omnisight-proxy/internal/config"
)

const (
	HeaderTenantID  = "X-Omnisight-Tenant-Id"
	HeaderNonce     = "X-Omnisight-Nonce"
	HeaderTimestamp = "X-Omnisight-Timestamp"
	HeaderSignature = "X-Omnisight-Signature"
)

// Authenticator verifies the SaaS caller's mTLS client certificate,
// tenant binding, HMAC-signed nonce, and replay window. Its replay cache is
// instance-local, not module-global; the KS.3 proxy is a single Go process.
type Authenticator struct {
	tenantID          string
	pinnedFingerprint string
	nonceKey          []byte
	nonceTTL          time.Duration
	now               func() time.Time

	mu   sync.Mutex
	seen map[string]time.Time
}

func New(cfg *config.Settings) (*Authenticator, error) {
	key, err := os.ReadFile(cfg.NonceHMACKeyFile)
	if err != nil {
		return nil, fmt.Errorf("read nonce HMAC key: %w", err)
	}
	key = []byte(strings.TrimSpace(string(key)))
	if len(key) < 32 {
		return nil, fmt.Errorf("nonce HMAC key must be at least 32 bytes")
	}
	return NewForTest(cfg.TenantID, cfg.PinnedClientCertSHA256, key, time.Duration(cfg.NonceTTLSeconds)*time.Second), nil
}

func NewForTest(tenantID string, pinnedFingerprint string, nonceKey []byte, nonceTTL time.Duration) *Authenticator {
	return &Authenticator{
		tenantID:          tenantID,
		pinnedFingerprint: normalizeFingerprint(pinnedFingerprint),
		nonceKey:          append([]byte(nil), nonceKey...),
		nonceTTL:          nonceTTL,
		now:               time.Now,
		seen:              make(map[string]time.Time),
	}
}

func (a *Authenticator) Middleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if err := a.Verify(r); err != nil {
			http.Error(w, err.Error(), http.StatusUnauthorized)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func (a *Authenticator) Verify(r *http.Request) error {
	if r.TLS == nil {
		return fmt.Errorf("mTLS required")
	}
	if len(r.TLS.PeerCertificates) == 0 {
		return fmt.Errorf("client certificate required")
	}
	if got := certificateFingerprint(r.TLS.PeerCertificates[0]); got != a.pinnedFingerprint {
		return fmt.Errorf("client certificate pin mismatch")
	}

	tenantID := r.Header.Get(HeaderTenantID)
	if tenantID != a.tenantID {
		return fmt.Errorf("tenant id mismatch")
	}
	nonce := r.Header.Get(HeaderNonce)
	if nonce == "" {
		return fmt.Errorf("nonce required")
	}
	timestamp, err := strconv.ParseInt(r.Header.Get(HeaderTimestamp), 10, 64)
	if err != nil {
		return fmt.Errorf("valid timestamp required")
	}
	signedAt := time.Unix(timestamp, 0)
	now := a.now()
	if signedAt.Before(now.Add(-a.nonceTTL)) || signedAt.After(now.Add(a.nonceTTL)) {
		return fmt.Errorf("timestamp outside nonce window")
	}
	if !hmac.Equal([]byte(r.Header.Get(HeaderSignature)), []byte(Sign(a.nonceKey, r.Method, r.URL.RequestURI(), tenantID, nonce, timestamp))) {
		return fmt.Errorf("bad nonce signature")
	}
	if err := a.markNonce(tenantID, nonce, now); err != nil {
		return err
	}
	return nil
}

func (a *Authenticator) markNonce(tenantID string, nonce string, now time.Time) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	for key, seenAt := range a.seen {
		if seenAt.Before(now.Add(-a.nonceTTL)) {
			delete(a.seen, key)
		}
	}
	key := tenantID + "\x00" + nonce
	if _, ok := a.seen[key]; ok {
		return fmt.Errorf("nonce replay rejected")
	}
	a.seen[key] = now
	return nil
}

func Sign(key []byte, method string, requestURI string, tenantID string, nonce string, timestamp int64) string {
	body := strings.Join([]string{
		method,
		requestURI,
		tenantID,
		nonce,
		strconv.FormatInt(timestamp, 10),
	}, "\n")
	mac := hmac.New(sha256.New, key)
	_, _ = mac.Write([]byte(body))
	return "sha256=" + hex.EncodeToString(mac.Sum(nil))
}

func ServerTLSConfig(cfg *config.Settings) (*tls.Config, error) {
	caPEM, err := os.ReadFile(cfg.ClientCAFile)
	if err != nil {
		return nil, fmt.Errorf("read client CA: %w", err)
	}
	clientCAs := x509.NewCertPool()
	if !clientCAs.AppendCertsFromPEM(caPEM) {
		return nil, fmt.Errorf("client CA file contains no PEM certificates")
	}
	return &tls.Config{
		MinVersion: tls.VersionTLS12,
		ClientAuth: tls.RequireAndVerifyClientCert,
		ClientCAs:  clientCAs,
	}, nil
}

func CertificateFingerprint(cert *x509.Certificate) string {
	return certificateFingerprint(cert)
}

func certificateFingerprint(cert *x509.Certificate) string {
	sum := sha256.Sum256(cert.Raw)
	return hex.EncodeToString(sum[:])
}

func normalizeFingerprint(value string) string {
	return strings.TrimPrefix(strings.ToLower(strings.TrimSpace(value)), "sha256:")
}
