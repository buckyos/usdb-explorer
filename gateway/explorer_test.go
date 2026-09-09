package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestExplorerRemovesUnsupportedRewardsWithoutChangingTransactionFee(t *testing.T) {
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"items":[{"hash":"0x1234","gas_used":"1","burnt_fees":"99","transaction_burnt_fee":"99","rewards":[{"reward":"1000"}],"fee":{"value":"100000000000000000000"},"metadata":{"burnt_fees":"contract metadata"}}],"height":9007199254740993}`))
	}))
	defer backend.Close()
	g, _ := newGateway("http://127.0.0.1:1", "0x1", "0x"+strings.Repeat("a", 64))
	g.explorerURL = backend.URL
	w := httptest.NewRecorder()
	g.ServeHTTP(w, httptest.NewRequest("GET", "/api/v2/blocks", nil))
	if w.Code != 200 {
		t.Fatal(w.Code, w.Body)
	}
	var value map[string]any
	d := json.NewDecoder(w.Body)
	d.UseNumber()
	if err := d.Decode(&value); err != nil {
		t.Fatal(err)
	}
	item := value["items"].([]any)[0].(map[string]any)
	if item["burnt_fees"] != nil || item["transaction_burnt_fee"] != nil || len(item["rewards"].([]any)) != 0 {
		t.Fatal("Ethereum reward assumptions escaped")
	}
	if item["fee"].(map[string]any)["value"] != "100000000000000000000" || value["height"].(json.Number).String() != "9007199254740993" {
		t.Fatal("valid chain values were changed")
	}
	if value["usdb_semantics"].(map[string]any)["rewards_supply"] != "not_qualified" {
		t.Fatal("missing semantic limitation")
	}
	if item["metadata"].(map[string]any)["burnt_fees"] != "contract metadata" {
		t.Fatal("contract metadata was changed")
	}
}

func TestExplorerDoesNotExposeAlternativeAPIOrWritePaths(t *testing.T) {
	g, _ := newGateway("http://127.0.0.1:1", "0x1", "0x"+strings.Repeat("a", 64))
	g.explorerURL = "http://127.0.0.1:1"
	for _, url := range []string{"/api/v2/stats", "/api/v2/proxy", "/api/v2/blocks/../stats"} {
		w := httptest.NewRecorder()
		g.ServeHTTP(w, httptest.NewRequest("GET", url, nil))
		if w.Code != 400 && w.Code != 404 {
			t.Fatal("alternative API forwarded", url, w.Code)
		}
	}
	w := httptest.NewRecorder()
	g.ServeHTTP(w, httptest.NewRequest("POST", "/api/v2/smart-contracts", nil))
	if w.Code != 405 {
		t.Fatal("write endpoint forwarded")
	}
}
