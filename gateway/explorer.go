package main

import (
	"bytes"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"path"
	"strings"
)

// sanitizeExplorer hides Ethereum-specific reward assumptions without rewriting gas paid by a user.
func sanitizeExplorer(value any) {
	switch v := value.(type) {
	case map[string]any:
		_, hasHash := v["hash"]
		_, hasGas := v["gas_used"]
		isChainRecord := hasHash && hasGas
		for key, child := range v {
			if key == "metadata" || key == "decoded_input" {
				continue
			}
			if !isChainRecord {
				sanitizeExplorer(child)
				continue
			}
			switch key {
			case "burnt_fees", "burnt_fees_percentage", "transaction_burnt_fee", "priority_fee":
				v[key] = nil
			case "rewards":
				v[key] = []any{}
			default:
				sanitizeExplorer(child)
			}
		}
	case []any:
		for _, child := range v {
			sanitizeExplorer(child)
		}
	}
}

func (g *gateway) explorer(w http.ResponseWriter, r *http.Request) {
	if path.Clean(r.URL.Path) != r.URL.Path {
		rpcError(w, 400, "Non-canonical explorer path")
		return
	}
	if r.Method != http.MethodGet || g.explorerURL == "" {
		rpcError(w, 405, "Explorer preview permits GET requests only")
		return
	}
	// Keep native supply/stats and alternative proxy APIs closed until their USDB semantics are qualified.
	part := strings.TrimPrefix(r.URL.Path, "/api/v2/")
	category := strings.SplitN(part, "/", 2)[0]
	switch category {
	case "blocks", "transactions", "addresses", "smart-contracts", "tokens", "search", "main-page":
	default:
		http.NotFound(w, r)
		return
	}
	select {
	case g.active <- struct{}{}:
		defer func() { <-g.active }()
	default:
		rpcError(w, 503, "Explorer concurrency limit reached")
		return
	}
	ip, _, _ := net.SplitHostPort(r.RemoteAddr)
	if !g.allow(ip, 1) {
		rpcError(w, 429, "Explorer rate limit reached")
		return
	}
	url := g.explorerURL + r.URL.RequestURI()
	request, err := http.NewRequestWithContext(r.Context(), http.MethodGet, url, nil)
	if err != nil {
		rpcError(w, 400, "Invalid explorer request")
		return
	}
	response, err := g.client.Do(request)
	if err != nil {
		rpcError(w, 502, "Explorer API unavailable")
		return
	}
	defer response.Body.Close()
	body, err := io.ReadAll(io.LimitReader(response.Body, maxResponse+1))
	if err != nil || len(body) > maxResponse {
		rpcError(w, 502, "Explorer response limit exceeded")
		return
	}
	var value any
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.UseNumber()
	if !json.Valid(body) || decoder.Decode(&value) != nil {
		rpcError(w, 502, "Explorer returned invalid JSON")
		return
	}
	sanitizeExplorer(value)
	if object, ok := value.(map[string]any); ok {
		object["usdb_semantics"] = map[string]string{"rewards_supply": "not_qualified", "fee_distribution": "not_qualified"}
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(response.StatusCode)
	_ = json.NewEncoder(w).Encode(value)
}
