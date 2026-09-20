package faucet

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/big"
	"net/http"
	"strings"
	"time"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/crypto"
)

// Chain is the bounded RPC interface shared by the worker and funding command.
type Chain interface {
	Call(context.Context, bool, string, any, any) error
}

// RPC keeps upstream errors and endpoints out of public responses and logs.
type RPC struct {
	ReadURL, BroadcastURL string
	Client                *http.Client
}

// NewRPC creates a timeout-bound client which refuses redirects.
func NewRPC(c Config) *RPC {
	return &RPC{c.ReadURL, c.BroadcastURL, &http.Client{Timeout: 10 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}}
}

// Call invokes only methods selected by service code; HTTP callers cannot select RPC methods.
func (r *RPC) Call(ctx context.Context, broadcast bool, method string, params, target any) error {
	endpoint := r.ReadURL
	if broadcast {
		endpoint = r.BroadcastURL
	}
	body, err := json.Marshal(map[string]any{"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
	if err != nil {
		return errors.New("RPC_REQUEST_INVALID")
	}
	req, err := http.NewRequestWithContext(ctx, "POST", endpoint, bytes.NewReader(body))
	if err != nil {
		return errors.New("RPC_UNAVAILABLE")
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := r.Client.Do(req)
	if err != nil {
		return errors.New("RPC_UNAVAILABLE")
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, (2<<20)+1))
	var envelope struct {
		Version string          `json:"jsonrpc"`
		ID      int             `json:"id"`
		Result  json.RawMessage `json:"result"`
		Error   json.RawMessage `json:"error"`
	}
	if err != nil || len(data) > 2<<20 || resp.StatusCode != 200 || json.Unmarshal(data, &envelope) != nil || envelope.Version != "2.0" || envelope.ID != 1 {
		return errors.New("RPC_UNAVAILABLE")
	}
	if len(envelope.Error) != 0 && string(envelope.Error) != "null" {
		return errors.New("RPC_REJECTED")
	}
	if len(envelope.Result) == 0 || json.Unmarshal(envelope.Result, target) != nil {
		return errors.New("RPC_INVALID_RESPONSE")
	}
	return nil
}

func quantity(s string) (*big.Int, error) {
	if !strings.HasPrefix(s, "0x") || len(s) < 3 || len(s) > 66 || (len(s) > 3 && s[2] == '0') {
		return nil, errors.New("RPC_INVALID_RESPONSE")
	}
	n, ok := new(big.Int).SetString(s[2:], 16)
	if !ok || n.Sign() < 0 {
		return nil, errors.New("RPC_INVALID_RESPONSE")
	}
	return n, nil
}

func number(ctx context.Context, rpc Chain, method string, params ...any) (*big.Int, error) {
	var raw string
	if err := rpc.Call(ctx, false, method, params, &raw); err != nil {
		return nil, err
	}
	return quantity(raw)
}

func identity(ctx context.Context, rpc Chain, c Config) error {
	want, _ := new(big.Int).SetString(c.ChainID, 10)
	for _, broadcast := range []bool{false, true} {
		var chain string
		var genesis *struct {
			Hash string `json:"hash"`
		}
		if err := rpc.Call(ctx, broadcast, "eth_chainId", []any{}, &chain); err != nil {
			return err
		}
		got, err := quantity(chain)
		if err != nil || got.Cmp(want) != 0 {
			return errors.New("WRONG_NETWORK")
		}
		if err = rpc.Call(ctx, broadcast, "eth_getBlockByNumber", []any{"0x0", false}, &genesis); err != nil {
			return err
		}
		if genesis == nil || !strings.EqualFold(genesis.Hash, c.Genesis) {
			return errors.New("WRONG_NETWORK")
		}
	}
	return nil
}

func address(s string) (common.Address, error) {
	if len(s) != 42 || !strings.HasPrefix(s, "0x") || !common.IsHexAddress(s) {
		return common.Address{}, errors.New("INVALID_ADDRESS")
	}
	a := common.HexToAddress(s)
	if a == (common.Address{}) {
		return a, errors.New("INVALID_ADDRESS")
	}
	if s != strings.ToLower(s) && s[2:] != strings.ToUpper(s[2:]) && s != a.Hex() {
		return a, errors.New("INVALID_ADDRESS")
	}
	return a, nil
}

func signedTransfer(ctx context.Context, rpc Chain, c Config, key *ecdsa.PrivateKey, to common.Address, amount *big.Int) (*types.Transaction, error) {
	if err := identity(ctx, rpc, c); err != nil {
		return nil, err
	}
	var syncing json.RawMessage
	if err := rpc.Call(ctx, false, "eth_syncing", []any{}, &syncing); err != nil {
		return nil, err
	}
	if string(syncing) != "false" {
		return nil, errors.New("NODE_SYNCING")
	}
	var code string
	if err := rpc.Call(ctx, false, "eth_getCode", []any{to.Hex(), "latest"}, &code); err != nil {
		return nil, err
	}
	if code != "0x" {
		return nil, errors.New("RECIPIENT_NOT_EOA")
	}
	from := crypto.PubkeyToAddress(key.PublicKey).Hex()
	latest, err := number(ctx, rpc, "eth_getTransactionCount", from, "latest")
	if err != nil {
		return nil, err
	}
	pending, err := number(ctx, rpc, "eth_getTransactionCount", from, "pending")
	if err != nil {
		return nil, err
	}
	if !latest.IsUint64() || latest.Cmp(pending) != 0 {
		return nil, errors.New("SENDER_HAS_PENDING_TRANSACTION")
	}
	gas, err := number(ctx, rpc, "eth_gasPrice")
	if err != nil {
		return nil, err
	}
	maxGas, _ := Units(c.MaxGasPriceGwei, 9)
	if gas.Sign() == 0 || gas.Cmp(maxGas) > 0 {
		return nil, errors.New("GAS_PRICE_TOO_HIGH")
	}
	balance, err := number(ctx, rpc, "eth_getBalance", from, "pending")
	if err != nil {
		return nil, err
	}
	reserve, _ := Units(c.GasReserve, 18)
	required := new(big.Int).Add(amount, new(big.Int).Mul(gas, big.NewInt(21000)))
	required.Add(required, reserve)
	if balance.Cmp(required) < 0 {
		return nil, errors.New("INSUFFICIENT_FUNDS")
	}
	chainID, _ := new(big.Int).SetString(c.ChainID, 10)
	return types.SignTx(types.NewTx(&types.LegacyTx{Nonce: latest.Uint64(), To: &to, Value: amount, Gas: 21000, GasPrice: gas}), types.NewEIP155Signer(chainID), key)
}

// receiptState requires canonical inclusion and the configured confirmation depth.
func receiptState(ctx context.Context, rpc Chain, hash string, confirmations uint64) (string, error) {
	var receipt *struct {
		Hash      string `json:"transactionHash"`
		BlockHash string `json:"blockHash"`
		Number    string `json:"blockNumber"`
		Status    string `json:"status"`
	}
	if err := rpc.Call(ctx, false, "eth_getTransactionReceipt", []any{hash}, &receipt); err != nil {
		return "", err
	}
	if receipt == nil {
		return "pending", nil
	}
	if !strings.EqualFold(receipt.Hash, hash) || !hashPattern.MatchString(receipt.BlockHash) {
		return "", errors.New("RPC_INVALID_RESPONSE")
	}
	n, err := quantity(receipt.Number)
	if err != nil || !n.IsUint64() {
		return "", errors.New("RPC_INVALID_RESPONSE")
	}
	var block *struct {
		Hash string `json:"hash"`
	}
	if err := rpc.Call(ctx, false, "eth_getBlockByNumber", []any{receipt.Number, false}, &block); err != nil {
		return "", err
	}
	if block == nil || !strings.EqualFold(block.Hash, receipt.BlockHash) {
		return "pending", nil
	}
	head, err := number(ctx, rpc, "eth_blockNumber")
	if err != nil {
		return "", err
	}
	required := new(big.Int).Add(n, new(big.Int).SetUint64(confirmations-1))
	if head.Cmp(required) < 0 {
		return "confirming", nil
	}
	switch receipt.Status {
	case "0x1":
		return "confirmed", nil
	case "0x0":
		return "failed", nil
	default:
		return "", errors.New("RPC_INVALID_RESPONSE")
	}
}

func broadcast(ctx context.Context, rpc Chain, c Config, j Job) error {
	if err := identity(ctx, rpc, c); err != nil {
		return err
	}
	var got string
	if err := rpc.Call(ctx, true, "eth_sendRawTransaction", []any{j.Raw}, &got); err != nil {
		return errors.New("BROADCAST_UNCERTAIN")
	}
	if !strings.EqualFold(got, j.Hash) {
		return errors.New("BROADCAST_UNCERTAIN")
	}
	return nil
}

func nonceText(n uint64) string { return fmt.Sprintf("%d", n) }
