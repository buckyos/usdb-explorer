package faucet

import (
	"crypto/subtle"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
)

var codes = map[string]bool{}

func init() {
	codes["WORKER_UNAVAILABLE"] = true
	codes["WORKER_ALREADY_RUNNING"] = true
	for _, code := range strings.Fields(`STARTING INVALID_AMOUNT INVALID_NETWORK INVALID_ENDPOINT INVALID_POLICY INVALID_CONFIG INVALID_ADDRESS INVALID_REQUEST_ID REQUEST_ID_CONFLICT ADDRESS_COOLDOWN ADDRESS_PENDING IP_RATE_LIMIT IP_DAILY_LIMIT QUEUE_FULL DAILY_BUDGET_EXHAUSTED INSUFFICIENT_FUNDS GAS_PRICE_TOO_HIGH NODE_SYNCING SENDER_HAS_PENDING_TRANSACTION RECIPIENT_NOT_EOA RPC_UNAVAILABLE RPC_REJECTED RPC_INVALID_RESPONSE RPC_REQUEST_INVALID WRONG_NETWORK BROADCAST_UNCERTAIN NONCE_CONFLICT NONCE_OR_STORAGE_CONFLICT NONCE_OR_REQUEST_CONFLICT RESERVATION_TOO_SMALL LEDGER_INVALID STORAGE_UNAVAILABLE STORAGE_IDENTITY_OR_IO_ERROR SIGNING_FAILED FUNDING_PENDING_USE_RETRY FUNDING_NOT_FOUND FUNDING_REQUIRES_DIFFERENT_ACCOUNT INVALID_PRIVATE_KEY WALLET_MISSING_RESTORE_BACKUP WALLET_PERMISSIONS_INVALID WALLET_READ_FAILED WALLET_INVALID WALLET_CREATE_FAILED WALLET_GENERATION_FAILED`) {
		codes[code] = true
	}
}

func safeCode(err error) string {
	if codes[err.Error()] {
		return err.Error()
	}
	return "STORAGE_UNAVAILABLE"
}

// ErrorCode is the only permitted representation of an error outside the service boundary.
func ErrorCode(err error) string { return safeCode(err) }

func reply(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func failure(w http.ResponseWriter, err error) {
	code := safeCode(err)
	status := http.StatusServiceUnavailable
	switch code {
	case "INVALID_ADDRESS", "INVALID_REQUEST_ID", "INVALID_AMOUNT":
		status = 400
	case "REQUEST_ID_CONFLICT", "ADDRESS_PENDING":
		status = 409
	case "ADDRESS_COOLDOWN", "IP_RATE_LIMIT", "IP_DAILY_LIMIT", "DAILY_BUDGET_EXHAUSTED":
		status = 429
	}
	if status == 429 {
		w.Header().Set("Retry-After", "60")
	}
	reply(w, status, map[string]any{"error": map[string]string{"code": code}})
}

// ServeHTTP exposes only status, fixed-policy claims and opaque claim receipts.
// The private proxy token authenticates the ingress which overwrites the client IP header.
func (s *Service) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path == "/healthz" && r.Method == "GET" {
		reply(w, 200, map[string]string{"status": "running"})
		return
	}
	if subtle.ConstantTimeCompare([]byte(r.Header.Get("X-USDB-Faucet-Proxy")), []byte(s.config.ProxyToken)) != 1 {
		http.NotFound(w, r)
		return
	}
	select {
	case s.active <- struct{}{}:
		defer func() { <-s.active }()
	default:
		reply(w, http.StatusServiceUnavailable, map[string]any{"error": map[string]string{"code": "SERVICE_BUSY"}})
		return
	}
	ip := r.Header.Get("X-Forwarded-For")
	parsed := net.ParseIP(ip)
	if parsed == nil {
		reply(w, 400, map[string]any{"error": map[string]string{"code": "INVALID_CLIENT_IP"}})
		return
	}
	// Treat an IPv6 /64 as one source to avoid trivial per-address rotation.
	if parsed.To4() == nil {
		ip = parsed.Mask(net.CIDRMask(64, 128)).String()
	} else {
		ip = parsed.String()
	}
	if err := s.rate(ip, "read", 120); err != nil {
		failure(w, err)
		return
	}
	if r.URL.RawQuery != "" {
		http.NotFound(w, r)
		return
	}
	const prefix = "/api/faucet/v1/"
	switch {
	case r.Method == "GET" && r.URL.Path == prefix+"status":
		reply(w, 200, s.snapshot())
	case r.Method == "GET" && strings.HasPrefix(r.URL.Path, prefix+"claims/"):
		id := strings.TrimPrefix(r.URL.Path, prefix+"claims/")
		if !strings.HasPrefix(id, "c_") || !requestID.MatchString(strings.TrimPrefix(id, "c_")) {
			http.NotFound(w, r)
			return
		}
		j, err := s.job(id)
		if err != nil {
			http.NotFound(w, r)
			return
		}
		reply(w, 200, j)
	case r.Method == "POST" && r.URL.Path == prefix+"claims":
		if r.Header.Get("Origin") != "" && !sameOrigin(r.Header.Get("Origin"), s.config.Origin) {
			reply(w, 403, map[string]any{"error": map[string]string{"code": "ORIGIN_MISMATCH"}})
			return
		}
		if strings.Split(r.Header.Get("Content-Type"), ";")[0] != "application/json" {
			reply(w, 415, map[string]any{"error": map[string]string{"code": "JSON_REQUIRED"}})
			return
		}
		if err := s.rate(ip, "claim", s.config.IPRequestsPerMinute); err != nil {
			failure(w, err)
			return
		}
		var input struct {
			ID      string `json:"request_id"`
			Address string `json:"address"`
		}
		d := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1024))
		d.DisallowUnknownFields()
		if d.Decode(&input) != nil || d.Decode(&struct{}{}) != io.EOF {
			failure(w, errors.New("INVALID_REQUEST_ID"))
			return
		}
		j, err := s.Claim(r.Context(), input.ID, input.Address, ip)
		if err != nil {
			failure(w, err)
			return
		}
		reply(w, 202, j)
	default:
		http.NotFound(w, r)
	}
}

func sameOrigin(a, b string) bool {
	canonical := func(raw string) string {
		u, err := url.Parse(raw)
		if err != nil || u.User != nil || u.Hostname() == "" || u.RawQuery != "" || u.Fragment != "" || (u.Path != "" && u.Path != "/") {
			return ""
		}
		port := u.Port()
		if port == "" {
			if u.Scheme == "https" {
				port = "443"
			} else if u.Scheme == "http" {
				port = "80"
			} else {
				return ""
			}
		}
		return u.Scheme + "://" + net.JoinHostPort(strings.ToLower(u.Hostname()), port)
	}
	x, y := canonical(a), canonical(b)
	return x != "" && x == y
}

// OperatorStatus includes funding recovery IDs but never raw transactions or keys.
func (s *Service) OperatorStatus() (map[string]any, error) {
	rows, err := s.db.Query("SELECT " + jobColumns + " FROM jobs WHERE kind='fund' ORDER BY created DESC LIMIT 20")
	if err != nil {
		return nil, errors.New("STORAGE_UNAVAILABLE")
	}
	defer rows.Close()
	funds := []Job{}
	for rows.Next() {
		j, err := scanJob(rows)
		if err != nil {
			return nil, err
		}
		j.ID = strings.TrimPrefix(j.ID, "f_")
		funds = append(funds, j)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return map[string]any{"faucet": s.snapshot(), "funding": funds}, nil
}
