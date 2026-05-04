package auth

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"testing"
	"time"

	"github.com/omnisight/productizer/omnisight-proxy/internal/config"
)

var nonceKey = []byte("0123456789abcdef0123456789abcdef")

func TestVerifyRejectsNonceReplay(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	client := newCertificate(t, nil, "client", x509.ExtKeyUsageClientAuth, now.Add(-time.Hour), now.Add(time.Hour))
	authenticator := NewForTest("tenant-a", CertificateFingerprint(client.leaf), nonceKey, 5*time.Minute)
	authenticator.now = func() time.Time { return now }

	req := signedRequest(t, authenticator, client.leaf, "nonce-1", now.Unix())
	if err := authenticator.Verify(req); err != nil {
		t.Fatalf("first verify failed: %v", err)
	}
	if err := authenticator.Verify(req); err == nil {
		t.Fatal("expected replayed nonce to be rejected")
	}
}

func TestVerifyRejectsBadSignatureWithoutConsumingNonce(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	client := newCertificate(t, nil, "client", x509.ExtKeyUsageClientAuth, now.Add(-time.Hour), now.Add(time.Hour))
	authenticator := NewForTest("tenant-a", CertificateFingerprint(client.leaf), nonceKey, 5*time.Minute)
	authenticator.now = func() time.Time { return now }

	req := signedRequest(t, authenticator, client.leaf, "nonce-1", now.Unix())
	req.Header.Set(HeaderSignature, "sha256=bad")
	if err := authenticator.Verify(req); err == nil {
		t.Fatal("expected bad signature to be rejected")
	}

	req.Header.Set(HeaderSignature, Sign(nonceKey, req.Method, req.URL.RequestURI(), "tenant-a", "nonce-1", now.Unix()))
	if err := authenticator.Verify(req); err != nil {
		t.Fatalf("valid retry should not be consumed by bad signature: %v", err)
	}
}

func TestMTLSHandshakeAndAuthMatrix(t *testing.T) {
	now := time.Now().UTC().Truncate(time.Second)
	ca := newCertificate(t, nil, "ca", x509.ExtKeyUsageAny, now.Add(-time.Hour), now.Add(time.Hour))
	serverCert := newCertificate(t, &ca, "127.0.0.1", x509.ExtKeyUsageServerAuth, now.Add(-time.Hour), now.Add(time.Hour))
	clientCert := newCertificate(t, &ca, "client", x509.ExtKeyUsageClientAuth, now.Add(-time.Hour), now.Add(time.Hour))
	otherClientCert := newCertificate(t, &ca, "other-client", x509.ExtKeyUsageClientAuth, now.Add(-time.Hour), now.Add(time.Hour))
	expiredClientCert := newCertificate(t, &ca, "expired-client", x509.ExtKeyUsageClientAuth, now.Add(-2*time.Hour), now.Add(-time.Hour))
	selfSignedClientCert := newCertificate(t, nil, "self-signed-client", x509.ExtKeyUsageClientAuth, now.Add(-time.Hour), now.Add(time.Hour))

	cfg := authConfig(t, ca.certPEM, CertificateFingerprint(clientCert.leaf))
	handler := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	authenticator, err := New(cfg)
	if err != nil {
		t.Fatalf("auth.New: %v", err)
	}
	tlsConfig, err := ServerTLSConfig(cfg)
	if err != nil {
		t.Fatalf("ServerTLSConfig: %v", err)
	}
	tlsConfig.Certificates = []tls.Certificate{serverCert.tlsCert}

	srv := httptest.NewUnstartedServer(authenticator.Middleware(handler))
	srv.TLS = tlsConfig
	srv.StartTLS()
	t.Cleanup(srv.Close)

	tests := []struct {
		name       string
		cert       certificate
		wantStatus int
		wantErr    bool
	}{
		{name: "valid", cert: clientCert, wantStatus: http.StatusOK},
		{name: "pinned-cert-mismatch", cert: otherClientCert, wantStatus: http.StatusUnauthorized},
		{name: "expired", cert: expiredClientCert, wantErr: true},
		{name: "self-signed", cert: selfSignedClientCert, wantErr: true},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			resp, err := signedClientRequest(t, srv.URL, ca.leaf, tt.cert, now)
			if tt.wantErr {
				if err == nil {
					resp.Body.Close()
					t.Fatal("expected TLS/auth request to fail")
				}
				return
			}
			if err != nil {
				t.Fatalf("request failed: %v", err)
			}
			defer resp.Body.Close()
			if resp.StatusCode != tt.wantStatus {
				t.Fatalf("status = %d, want %d", resp.StatusCode, tt.wantStatus)
			}
		})
	}
}

type certificate struct {
	tlsCert tls.Certificate
	leaf    *x509.Certificate
	certPEM []byte
	keyPEM  []byte
	key     *ecdsa.PrivateKey
}

func signedRequest(t *testing.T, authenticator *Authenticator, clientCert *x509.Certificate, nonce string, timestamp int64) *http.Request {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, "/auth/verify", nil)
	req.TLS = &tls.ConnectionState{PeerCertificates: []*x509.Certificate{clientCert}}
	req.Header.Set(HeaderTenantID, authenticator.tenantID)
	req.Header.Set(HeaderNonce, nonce)
	req.Header.Set(HeaderTimestamp, strconv.FormatInt(timestamp, 10))
	req.Header.Set(HeaderSignature, Sign(nonceKey, req.Method, req.URL.RequestURI(), authenticator.tenantID, nonce, timestamp))
	return req
}

func signedClientRequest(t *testing.T, url string, caCert *x509.Certificate, clientCert certificate, now time.Time) (*http.Response, error) {
	t.Helper()
	roots := x509.NewCertPool()
	roots.AddCert(caCert)
	httpClient := &http.Client{
		Transport: &http.Transport{
			TLSClientConfig: &tls.Config{
				MinVersion:   tls.VersionTLS12,
				RootCAs:      roots,
				Certificates: []tls.Certificate{clientCert.tlsCert},
			},
		},
		Timeout: 5 * time.Second,
	}
	req, err := http.NewRequest(http.MethodGet, url, nil)
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	nonce := "nonce-" + clientCert.leaf.Subject.CommonName
	req.Header.Set(HeaderTenantID, "tenant-a")
	req.Header.Set(HeaderNonce, nonce)
	req.Header.Set(HeaderTimestamp, strconv.FormatInt(now.Unix(), 10))
	req.Header.Set(HeaderSignature, Sign(nonceKey, req.Method, req.URL.RequestURI(), "tenant-a", nonce, now.Unix()))
	return httpClient.Do(req)
}

func authConfig(t *testing.T, caPEM []byte, pinnedFingerprint string) *config.Settings {
	t.Helper()
	dir := t.TempDir()
	caPath := filepath.Join(dir, "ca.crt")
	keyPath := filepath.Join(dir, "nonce.key")
	if err := os.WriteFile(caPath, caPEM, 0o600); err != nil {
		t.Fatalf("write ca: %v", err)
	}
	if err := os.WriteFile(keyPath, nonceKey, 0o600); err != nil {
		t.Fatalf("write key: %v", err)
	}
	cfg := config.ForTest()
	cfg.AuthEnabled = true
	cfg.TenantID = "tenant-a"
	cfg.ServerCertFile = "server.crt"
	cfg.ServerKeyFile = "server.key"
	cfg.ClientCAFile = caPath
	cfg.PinnedClientCertSHA256 = pinnedFingerprint
	cfg.NonceHMACKeyFile = keyPath
	return cfg
}

func newCertificate(t *testing.T, ca *certificate, commonName string, usage x509.ExtKeyUsage, notBefore time.Time, notAfter time.Time) certificate {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("generate key: %v", err)
	}
	serialLimit := new(big.Int).Lsh(big.NewInt(1), 128)
	serial, err := rand.Int(rand.Reader, serialLimit)
	if err != nil {
		t.Fatalf("generate serial: %v", err)
	}
	template := &x509.Certificate{
		SerialNumber: serial,
		Subject: pkix.Name{
			CommonName: commonName,
		},
		NotBefore: notBefore,
		NotAfter:  notAfter,
		KeyUsage:  x509.KeyUsageDigitalSignature,
		ExtKeyUsage: []x509.ExtKeyUsage{
			usage,
		},
	}
	parent := template
	signer := key
	if ca == nil {
		template.IsCA = true
		template.KeyUsage |= x509.KeyUsageCertSign
		template.BasicConstraintsValid = true
	} else {
		parent = ca.leaf
		signer = ca.key
	}
	if usage == x509.ExtKeyUsageServerAuth {
		template.IPAddresses = []net.IP{net.ParseIP("127.0.0.1")}
	}
	raw, err := x509.CreateCertificate(rand.Reader, template, parent, &key.PublicKey, signer)
	if err != nil {
		t.Fatalf("create cert: %v", err)
	}
	leaf, err := x509.ParseCertificate(raw)
	if err != nil {
		t.Fatalf("parse cert: %v", err)
	}
	keyBytes, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		t.Fatalf("marshal key: %v", err)
	}
	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: raw})
	keyPEM := pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyBytes})
	tlsCert, err := tls.X509KeyPair(certPEM, keyPEM)
	if err != nil {
		t.Fatalf("tls key pair: %v", err)
	}
	tlsCert.Leaf = leaf
	return certificate{
		tlsCert: tlsCert,
		leaf:    leaf,
		certPEM: certPEM,
		keyPEM:  keyPEM,
		key:     key,
	}
}
