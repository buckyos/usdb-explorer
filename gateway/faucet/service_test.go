package faucet

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math/big"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/ethereum/go-ethereum/core/types"
)

const recipient = "0x1111111111111111111111111111111111111111"
const minerKey = "0000000000000000000000000000000000000000000000000000000000000001"

type fakeChain struct {
	mu             sync.Mutex
	config         Config
	nonces         map[string]uint64
	receipts       map[string]any
	sent           []*types.Transaction
	head           uint64
	balance        string
	gas            string
	failMethod     string
	wrong          bool
	wrongBroadcast bool
	reorg          bool
	contract       bool
	known          bool
}

func testConfig() Config {
	return Config{ChainID: "202608250", Genesis: "0x" + strings.Repeat("a", 64), ReadURL: "http://read.invalid", BroadcastURL: "http://write.invalid", Origin: "http://explorer.invalid", ProxyToken: strings.Repeat("b", 64), ClaimAmount: "1", DailyBudget: "100", GasReserve: "0.01", MaxGasPriceGwei: "100", CooldownSeconds: 86400, IPDailyClaims: 10, IPRequestsPerMinute: 10, Confirmations: 3}
}

func newChain(c Config) *fakeChain {
	return &fakeChain{config: c, nonces: map[string]uint64{}, receipts: map[string]any{}, head: 12, balance: "0x3635c9adc5dea00000", gas: "0x3b9aca00"}
}

func (f *fakeChain) Call(_ context.Context, writer bool, method string, params any, target any) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	p := params.([]any)
	var result any
	switch method {
	case "eth_chainId":
		id, _ := new(big.Int).SetString(f.config.ChainID, 10)
		if f.wrong || (writer && f.wrongBroadcast) {
			id.SetInt64(1)
		}
		result = "0x" + id.Text(16)
	case "eth_getBlockByNumber":
		hash := f.config.Genesis
		if p[0] != "0x0" {
			hash = "0x" + strings.Repeat("c", 64)
			if f.reorg {
				hash = "0x" + strings.Repeat("d", 64)
			}
		}
		result = map[string]string{"hash": hash}
	case "eth_syncing":
		result = false
	case "eth_getCode":
		result = "0x"
		if f.contract {
			result = "0x1234"
		}
	case "eth_getBalance":
		result = f.balance
	case "eth_gasPrice":
		result = f.gas
	case "eth_getTransactionCount":
		result = fmt.Sprintf("0x%x", f.nonces[p[0].(string)])
	case "eth_blockNumber":
		result = fmt.Sprintf("0x%x", f.head)
	case "eth_getTransactionReceipt":
		result = f.receipts[p[0].(string)]
	case "eth_getTransactionByHash":
		if f.known {
			result = map[string]string{"hash": p[0].(string)}
		}
	case "eth_sendRawTransaction":
		data, _ := hex.DecodeString(strings.TrimPrefix(p[0].(string), "0x"))
		var tx types.Transaction
		if tx.UnmarshalBinary(data) != nil {
			return errors.New("bad transaction")
		}
		f.sent = append(f.sent, &tx)
		result = tx.Hash().Hex()
	default:
		return fmt.Errorf("unexpected RPC %s", method)
	}
	if f.failMethod == method {
		return errors.New("RPC_UNAVAILABLE")
	}
	data, _ := json.Marshal(result)
	return json.Unmarshal(data, target)
}

func (f *fakeChain) mine(tx *types.Transaction, status string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	from, _ := types.Sender(types.NewEIP155Signer(tx.ChainId()), tx)
	f.nonces[from.Hex()] = tx.Nonce() + 1
	f.receipts[tx.Hash().Hex()] = map[string]string{"transactionHash": tx.Hash().Hex(), "blockHash": "0x" + strings.Repeat("c", 64), "blockNumber": "0xa", "status": status}
}

func setup(t *testing.T) (*Service, *fakeChain, string) {
	t.Helper()
	c := testConfig()
	rpc := newChain(c)
	dir := t.TempDir()
	s, err := Open(dir, c, rpc)
	if err != nil {
		t.Fatal(err)
	}
	s.now = func() time.Time { return time.Date(2026, 9, 19, 12, 0, 0, 0, time.UTC) }
	if err = s.Refresh(context.Background()); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { s.Close() })
	return s, rpc, dir
}

func claim(t *testing.T, s *Service, id string) Job {
	t.Helper()
	j, err := s.Claim(context.Background(), id, recipient, "192.0.2.1")
	if err != nil {
		t.Fatal(err)
	}
	return j
}

func TestConcurrentIdempotencyCooldownAndDurableWallet(t *testing.T) {
	s, rpc, dir := setup(t)
	ctx := context.Background()
	var wg sync.WaitGroup
	errCh := make(chan error, 32)
	for i := 0; i < 32; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, err := s.Claim(ctx, "same-request-012345", recipient, "192.0.2.1")
			errCh <- err
		}()
	}
	wg.Wait()
	close(errCh)
	for err := range errCh {
		if err != nil {
			t.Fatal(err)
		}
	}
	var count int
	s.db.QueryRow("SELECT COUNT(*) FROM jobs").Scan(&count)
	if count != 1 {
		t.Fatalf("created %d claims", count)
	}
	if _, err := s.Claim(ctx, "other-request-012345", recipient, "192.0.2.2"); err == nil || err.Error() != "ADDRESS_COOLDOWN" {
		t.Fatal(err)
	}
	original := s.sender()
	s.Close()
	restored, err := Open(dir, s.config, rpc)
	if err != nil {
		t.Fatal(err)
	}
	defer restored.Close()
	if restored.sender() != original {
		t.Fatal("wallet changed after restart")
	}
	if _, err := restored.job("c_same-request-012345"); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(filepath.Join(dir, "wallet.key"), 0644); err != nil {
		t.Fatal(err)
	}
	if _, err := Open(dir, s.config, rpc); err == nil {
		t.Fatal("world-readable key accepted")
	}
}

func TestBudgetAndIPReservationsSurviveMidnightAndRestart(t *testing.T) {
	s, _, _ := setup(t)
	ctx := context.Background()
	s.config.DailyBudget = "2"
	claim(t, s, "budget-request-012345")
	for _, day := range []int{19, 20} {
		s.now = func() time.Time { return time.Date(2026, 9, day, 12, 0, 0, 0, time.UTC) }
		if _, err := s.Claim(ctx, "second-request-012345", "0x2222222222222222222222222222222222222222", "192.0.2.2"); err == nil || err.Error() != "DAILY_BUDGET_EXHAUSTED" {
			t.Fatalf("day %d: %v", day, err)
		}
	}
	s.config.DailyBudget = "100"
	s.now = func() time.Time { return time.Date(2026, 9, 19, 13, 0, 0, 0, time.UTC) }
	s.config.IPDailyClaims = 1
	if _, err := s.Claim(ctx, "third-request-012345", "0x2222222222222222222222222222222222222222", "192.0.2.1"); err == nil || err.Error() != "IP_DAILY_LIMIT" {
		t.Fatal(err)
	}
}

func TestBroadcastTimeoutRestartAndReorgReuseSignedBytes(t *testing.T) {
	s, rpc, dir := setup(t)
	ctx := context.Background()
	j := claim(t, s, "timeout-request-012345")
	rpc.failMethod = "eth_sendRawTransaction"
	if err := s.Step(ctx); err == nil || err.Error() != "BROADCAST_UNCERTAIN" {
		t.Fatal(err)
	}
	j, err := s.job(j.ID)
	if err != nil || j.Raw == "" || j.Hash == "" {
		t.Fatalf("missing recovery record: %+v %v", j, err)
	}
	s.Close()
	restored, err := Open(dir, s.config, rpc)
	if err != nil {
		t.Fatal(err)
	}
	defer restored.Close()
	rpc.failMethod = ""
	if err = restored.Step(ctx); err != nil {
		t.Fatal(err)
	}
	if len(rpc.sent) != 2 || rpc.sent[0].Hash() != rpc.sent[1].Hash() {
		t.Fatal("restart created another transaction")
	}
	tx := rpc.sent[0]
	from, err := types.Sender(types.NewEIP155Signer(tx.ChainId()), tx)
	if err != nil || from.Hex() != restored.sender() || tx.To().Hex() != recipient || tx.Value().String() != "1000000000000000000" || tx.Gas() != 21000 {
		t.Fatal("invalid signed transfer")
	}
	rpc.mine(tx, "0x1")
	rpc.head = 10
	if err = restored.Step(ctx); err != nil {
		t.Fatal(err)
	}
	current, _ := restored.job(j.ID)
	if current.Status != "confirming" {
		t.Fatal(current.Status)
	}
	// A receipt on a noncanonical block must not qualify as confirmed or create a new payout.
	rpc.reorg = true
	rpc.nonces[from.Hex()] = 0
	if err = restored.Step(ctx); err != nil {
		t.Fatal(err)
	}
	current, _ = restored.job(j.ID)
	if current.Status != "pending" || rpc.sent[len(rpc.sent)-1].Hash() != tx.Hash() {
		t.Fatal(current)
	}
	rpc.reorg = false
	rpc.head = 12
	rpc.nonces[from.Hex()] = 1
	if err = restored.Step(ctx); err != nil {
		t.Fatal(err)
	}
	current, _ = restored.job(j.ID)
	if current.Status != "confirmed" {
		t.Fatal(current)
	}
	before := len(rpc.sent)
	if err = restored.Step(ctx); err != nil || len(rpc.sent) != before {
		t.Fatal("completed claim broadcast again")
	}
}

func TestPersistBeforeFirstBroadcastAndRejectUnknownNonceConsumption(t *testing.T) {
	s, rpc, _ := setup(t)
	ctx := context.Background()
	j := claim(t, s, "crash-request-012345")
	rpc.failMethod = "eth_getTransactionReceipt"
	if err := s.Step(ctx); err == nil {
		t.Fatal("receipt RPC should fail")
	}
	j, _ = s.job(j.ID)
	if j.Raw == "" || len(rpc.sent) != 0 {
		t.Fatal("not durably prepared before broadcast")
	}
	rpc.failMethod = ""
	rpc.nonces[s.sender()] = 1
	if err := s.Step(ctx); err == nil || err.Error() != "NONCE_CONFLICT" {
		t.Fatal(err)
	}
	if len(rpc.sent) != 0 {
		t.Fatal("replaced a transaction of unknown outcome")
	}
}

func TestFundingIsOneShotJournaledAndRetryDoesNotNeedMinerKey(t *testing.T) {
	s, rpc, dir := setup(t)
	ctx := context.Background()
	rpc.failMethod = "eth_sendRawTransaction"
	j, err := s.Fund(ctx, "refill-1", "10", minerKey)
	if err == nil || err.Error() != "BROADCAST_UNCERTAIN" || j.Hash == "" {
		t.Fatalf("%+v %v", j, err)
	}
	rpc.failMethod = ""
	got, err := s.RetryFund(ctx, "refill-1")
	if err != nil || got.Hash != j.Hash {
		t.Fatalf("%+v %v", got, err)
	}
	if len(rpc.sent) != 2 || rpc.sent[0].Hash() != rpc.sent[1].Hash() {
		t.Fatal("funding retry duplicated payment")
	}
	if _, err = s.Fund(ctx, "refill-2", "10", minerKey); err == nil {
		t.Fatal("new funding accepted while old one is unresolved")
	}
	if _, err = s.Fund(ctx, "refill-1", "11", minerKey); err == nil || err.Error() != "REQUEST_ID_CONFLICT" {
		t.Fatal(err)
	}
	rpc.mine(rpc.sent[0], "0x1")
	got, err = s.RetryFund(ctx, "refill-1")
	if err != nil || got.Status != "confirmed" {
		t.Fatalf("%+v %v", got, err)
	}
	if _, err = s.Fund(ctx, "refill-2", "10", minerKey); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"wallet.key", "faucet.sqlite", "faucet.sqlite-wal"} {
		data, err := os.ReadFile(filepath.Join(dir, name))
		if err == nil && strings.Contains(string(data), minerKey) {
			t.Fatal("miner key persisted")
		}
	}
	public, _ := json.Marshal(j)
	if strings.Contains(string(public), j.Raw) || strings.Contains(string(public), minerKey) {
		t.Fatal("secret or raw transaction exposed")
	}
}

func TestNetworkBalanceGasAndWalletIdentityGates(t *testing.T) {
	for _, mode := range []string{"wrong-read", "wrong-broadcast", "balance", "gas", "contract"} {
		t.Run(mode, func(t *testing.T) {
			s, rpc, _ := setup(t)
			claim(t, s, "gated-request-012345")
			switch mode {
			case "wrong-read":
				rpc.wrong = true
			case "wrong-broadcast":
				rpc.wrongBroadcast = true
			case "balance":
				rpc.balance = "0x0"
			case "gas":
				rpc.gas = "0xffffffffffff"
			case "contract":
				rpc.contract = true
			}
			err := s.Step(context.Background())
			if mode != "contract" && err == nil {
				t.Fatal("gate accepted")
			}
			if len(rpc.sent) != 0 {
				t.Fatal("gate broadcast")
			}
		})
	}
	s, rpc, dir := setup(t)
	s.Close()
	c := s.config
	c.Genesis = "0x" + strings.Repeat("d", 64)
	if _, err := Open(dir, c, rpc); err == nil {
		t.Fatal("rebound ledger to another network")
	}
	if err := os.Remove(filepath.Join(dir, "wallet.key")); err != nil {
		t.Fatal(err)
	}
	if _, err := Open(dir, s.config, rpc); err == nil || err.Error() != "WALLET_MISSING_RESTORE_BACKUP" {
		t.Fatal(err)
	}
}

func TestHTTPTrustLimitsAndPrivateSurface(t *testing.T) {
	s, _, _ := setup(t)
	request := func(method, path, body, token, ip string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, path, strings.NewReader(body))
		r.Header.Set("X-USDB-Faucet-Proxy", token)
		r.Header.Set("X-Forwarded-For", ip)
		r.Header.Set("Content-Type", "application/json")
		r.Header.Set("Origin", s.config.Origin)
		w := httptest.NewRecorder()
		s.ServeHTTP(w, r)
		return w
	}
	if w := request("GET", "/api/faucet/v1/status", "", "", "192.0.2.1"); w.Code != 404 {
		t.Fatal(w.Code)
	}
	if w := request("GET", "/api/faucet/v1/status", "", s.config.ProxyToken, "192.0.2.1, 1.2.3.4"); w.Code != 400 {
		t.Fatal(w.Code)
	}
	body := `{"request_id":"http-request-012345","address":"` + recipient + `"}`
	if w := request("POST", "/api/faucet/v1/claims", body, s.config.ProxyToken, "192.0.2.1"); w.Code != 202 {
		t.Fatal(w.Code, w.Body.String())
	}
	if w := request("GET", "/api/faucet/v1/claims/c_http-request-012345", "", s.config.ProxyToken, "192.0.2.1"); w.Code != 200 || strings.Contains(w.Body.String(), "sender") {
		t.Fatal(w.Body.String())
	}
	for _, path := range []string{"/api/faucet/v1/fund", "/api/faucet/v1/claims/f_refill", "/wallet.key"} {
		if w := request("GET", path, "", s.config.ProxyToken, "192.0.2.1"); w.Code != 404 {
			t.Fatal(path, w.Code)
		}
	}
	for i := 0; i < 10; i++ {
		request("POST", "/api/faucet/v1/claims", body, s.config.ProxyToken, "192.0.2.2")
	}
	if w := request("POST", "/api/faucet/v1/claims", body, s.config.ProxyToken, "192.0.2.2"); w.Code != 429 || !strings.Contains(w.Body.String(), "IP_RATE_LIMIT") {
		t.Fatal(w.Code, w.Body.String())
	}
}

func TestRPCSanitizesSecretsAndRejectsRedirects(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"jsonrpc":"2.0","id":1,"error":{"message":"secret-key-and-private-url","data":"sensitive"}}`))
	}))
	defer upstream.Close()
	c := testConfig()
	c.ReadURL = upstream.URL
	c.BroadcastURL = upstream.URL
	var result string
	err := NewRPC(c).Call(context.Background(), false, "eth_chainId", []any{}, &result)
	if err == nil || err.Error() != "RPC_REJECTED" {
		t.Fatal(err)
	}
	redirect := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, upstream.URL, http.StatusTemporaryRedirect)
	}))
	defer redirect.Close()
	c.ReadURL = redirect.URL
	if err = NewRPC(c).Call(context.Background(), false, "eth_chainId", []any{}, &result); err == nil || err.Error() != "RPC_UNAVAILABLE" {
		t.Fatal(err)
	}
}

func TestExactAmounts(t *testing.T) {
	for _, invalid := range []string{"0", "-1", "1e18", "01", "0.0000000000000000001", "NaN"} {
		if _, err := Units(invalid, 18); err == nil {
			t.Fatal(invalid)
		}
	}
	n, err := Units("9007199254740993.000000000000000001", 18)
	if err != nil || n.String() != "9007199254740993000000000000000001" {
		t.Fatal(n, err)
	}
}

func TestConcurrentDifferentAddressesCannotOverdrawBudget(t *testing.T) {
	s, _, _ := setup(t)
	s.config.DailyBudget = "2"
	var wg sync.WaitGroup
	results := make(chan error, 20)
	for i := 1; i <= 20; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			_, err := s.Claim(context.Background(), fmt.Sprintf("budget-concurrent-%03d", i), fmt.Sprintf("0x%040x", i), fmt.Sprintf("192.0.2.%d", i))
			results <- err
		}(i)
	}
	wg.Wait()
	close(results)
	accepted := 0
	for err := range results {
		if err == nil {
			accepted++
		} else if err.Error() != "DAILY_BUDGET_EXHAUSTED" {
			t.Fatal(err)
		}
	}
	if accepted != 1 {
		t.Fatalf("accepted %d claims against a one-claim budget", accepted)
	}
}

func TestSettledOldQueueStillChargesTodaysBudget(t *testing.T) {
	s, rpc, _ := setup(t)
	s.config.DailyBudget = "2"
	claim(t, s, "day-rollover-012345")
	ctx := context.Background()
	if err := s.Step(ctx); err != nil {
		t.Fatal(err)
	}
	s.now = func() time.Time { return time.Date(2026, 9, 20, 12, 0, 0, 0, time.UTC) }
	rpc.mine(rpc.sent[0], "0x1")
	if err := s.Step(ctx); err != nil {
		t.Fatal(err)
	}
	_, err := s.Claim(ctx, "another-day-012345", "0x2222222222222222222222222222222222222222", "192.0.2.2")
	if err == nil || err.Error() != "DAILY_BUDGET_EXHAUSTED" {
		t.Fatal(err)
	}
}

func TestOriginDefaultPortsAndDistinctSites(t *testing.T) {
	for _, pair := range [][2]string{{"http://EXPLORER.invalid", "http://explorer.invalid:80"}, {"https://explorer.invalid:443", "https://explorer.invalid"}} {
		if !sameOrigin(pair[0], pair[1]) {
			t.Fatal(pair)
		}
	}
	for _, origin := range []string{"http://attacker.invalid", "null", "http://explorer.invalid:28080", "http://explorer.invalid/path", "http://user@explorer.invalid"} {
		if sameOrigin(origin, "http://explorer.invalid") {
			t.Fatal(origin)
		}
	}
}

func TestKnownPendingTransactionWaitsWithoutRepeatedBroadcast(t *testing.T) {
	s, rpc, _ := setup(t)
	j := claim(t, s, "known-pending-012345")
	ctx := context.Background()
	if err := s.Step(ctx); err != nil {
		t.Fatal(err)
	}
	rpc.known = true
	if err := s.Step(ctx); err != nil {
		t.Fatal(err)
	}
	if len(rpc.sent) != 1 {
		t.Fatal("already-known transaction broadcast again")
	}
	current, err := s.job(j.ID)
	if err != nil || current.Status != "pending" {
		t.Fatal(current, err)
	}
}

func TestStaleLedgerOrExternalWalletSpendingStopsNewPayouts(t *testing.T) {
	s, rpc, _ := setup(t)
	claim(t, s, "stale-ledger-012345")
	rpc.nonces[s.sender()] = 1
	if err := s.Step(context.Background()); err == nil || err.Error() != "NONCE_CONFLICT" {
		t.Fatal(err)
	}
	if len(rpc.sent) != 0 {
		t.Fatal("paid from a wallet with an unaccounted outgoing transaction")
	}
}
