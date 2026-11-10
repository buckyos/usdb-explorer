package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
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

func TestExplorerForwardsBlockchainListsAndPagination(t *testing.T) {
	internal := `{"items":[{"transaction_hash":"0x1234","index":2,"block_number":154402,"type":"call","value":"1000000000000000001"}],"next_page_params":{"block_number":154402,"transaction_index":0,"index":2,"items_count":50}}`
	accounts := `{"items":[{"hash":"0x1111111111111111111111111111111111111111","coin_balance":"10989978999999853000","transactions_count":"4"}],"next_page_params":{"hash":"0x1111111111111111111111111111111111111111","fetched_coin_balance":"10989978999999853000","items_count":50},"total_supply":"0","exchange_rate":null}`
	for _, tc := range []struct {
		uri  string
		body string
	}{
		{"/api/v2/internal-transactions", internal},
		{"/api/v2/internal-transactions/", internal},
		{"/api/v2/internal-transactions?transaction_hash=0x1234", internal},
		{"/api/v2/internal-transactions?block_number=154402&transaction_index=0&index=2&items_count=50", internal},
		{"/api/v2/addresses", accounts},
		{"/api/v2/addresses/", accounts},
		{"/api/v2/addresses/?hash=0x1111111111111111111111111111111111111111&fetched_coin_balance=10989978999999853000&items_count=50", accounts},
		{"/api/v2/addresses/0x1111111111111111111111111111111111111111/internal-transactions?filter=to%20%7C%20from", internal},
		{"/api/v2/transactions/0x1234/internal-transactions", internal},
	} {
		t.Run(tc.uri, func(t *testing.T) {
			requests := make(chan string, 1)
			backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				requests <- r.Method + " " + r.RequestURI
				w.Header().Set("Content-Type", "application/json")
				_, _ = w.Write([]byte(tc.body))
			}))
			defer backend.Close()
			g, _ := newGateway("http://127.0.0.1:1", "0x1", "0x"+strings.Repeat("a", 64))
			g.explorerURL = backend.URL
			w := httptest.NewRecorder()
			g.ServeHTTP(w, httptest.NewRequest(http.MethodGet, tc.uri, nil))
			if w.Code != http.StatusOK {
				t.Fatal(w.Code, w.Body)
			}
			if got := <-requests; got != "GET "+tc.uri {
				t.Fatalf("backend request changed: %q", got)
			}
			var got, want map[string]any
			if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
				t.Fatal(err)
			}
			if err := json.Unmarshal([]byte(tc.body), &want); err != nil {
				t.Fatal(err)
			}
			if got["usdb_semantics"].(map[string]any)["rewards_supply"] != "not_qualified" {
				t.Fatal("missing semantic limitation")
			}
			delete(got, "usdb_semantics")
			if !reflect.DeepEqual(got, want) {
				t.Fatalf("list data or pagination changed: got %v, want %v", got, want)
			}
		})
	}
}

func TestExplorerDoesNotExposeAlternativeAPIOrWritePaths(t *testing.T) {
	g, _ := newGateway("http://127.0.0.1:1", "0x1", "0x"+strings.Repeat("a", 64))
	g.explorerURL = "http://127.0.0.1:1"
	for _, tc := range []struct {
		method string
		uri    string
		status int
	}{
		{"GET", "/api/v2/stats", 404},
		{"GET", "/api/v2/stats/", 404},
		{"GET", "/api/v2/proxy", 404},
		{"GET", "/api/v2/proxy/", 404},
		{"GET", "/api/v2/config/backend-version", 404},
		{"GET", "/api/v2/internal-transactions-extra", 404},
		{"GET", "/api/v2/blocks/../stats", 400},
		{"GET", "/api/v2/addresses/../stats/", 400},
		{"GET", "/api/v2/addresses/./", 400},
		{"GET", "/api/v2/addresses//", 400},
		{"GET", "/api/v2/addresses//internal-transactions/", 400},
		{"GET", "/api/v2/internal-transactions/../proxy/", 400},
		{"GET", "/api/v2/internal-transactions//", 400},
		{"GET", "/api/v2//internal-transactions/", 400},
		{"GET", "/api/v2/addresses/%2e%2e/stats/", 400},
		{"GET", "/api/v2/addresses/%2E/", 400},
		{"GET", "/api/v2/addresses/%2f", 400},
		{"GET", "/api/v2/internal-transactions%2f%2f", 400},
		{"POST", "/api/v2/smart-contracts", 405},
		{"POST", "/api/v2/addresses/", 405},
		{"POST", "/api/v2/internal-transactions", 405},
		{"PUT", "/api/v2/internal-transactions/", 405},
		{"DELETE", "/api/v2/internal-transactions/", 405},
	} {
		t.Run(tc.method+" "+tc.uri, func(t *testing.T) {
			w := httptest.NewRecorder()
			g.ServeHTTP(w, httptest.NewRequest(tc.method, tc.uri, nil))
			if w.Code != tc.status {
				t.Fatalf("got %d, want %d: %s", w.Code, tc.status, w.Body)
			}
		})
	}
}
