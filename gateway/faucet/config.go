// Package faucet implements an optional, independently funded testnet faucet.
package faucet

import (
	"encoding/json"
	"errors"
	"math/big"
	"net/url"
	"regexp"
	"strings"
)

// Config contains policy and network identity, never an operator signing key.
type Config struct {
	ChainID             string `json:"chain_id"`
	Genesis             string `json:"genesis_hash"`
	ReadURL             string `json:"read_url"`
	BroadcastURL        string `json:"broadcast_url"`
	Origin              string `json:"origin"`
	ProxyToken          string `json:"proxy_token"`
	ClaimAmount         string `json:"claim_amount"`
	DailyBudget         string `json:"daily_budget"`
	GasReserve          string `json:"gas_reserve"`
	MaxGasPriceGwei     string `json:"max_gas_price_gwei"`
	CooldownSeconds     int64  `json:"cooldown_seconds"`
	IPDailyClaims       int    `json:"ip_daily_claims"`
	IPRequestsPerMinute int    `json:"ip_requests_per_minute"`
	Confirmations       uint64 `json:"confirmations"`
}

var decimal = regexp.MustCompile(`^(0|[1-9][0-9]{0,30})(\.[0-9]+)?$`)
var hashPattern = regexp.MustCompile(`^0x[0-9a-fA-F]{64}$`)
var requestID = regexp.MustCompile(`^[A-Za-z0-9_-]{16,80}$`)
var fundingID = regexp.MustCompile(`^[A-Za-z0-9_-]{1,80}$`)

// Units parses decimal amounts without floating point rounding or exponents.
func Units(value string, decimals int) (*big.Int, error) {
	if !decimal.MatchString(value) {
		return nil, errors.New("INVALID_AMOUNT")
	}
	parts := strings.Split(value, ".")
	fraction := ""
	if len(parts) == 2 {
		fraction = parts[1]
	}
	if len(fraction) > decimals {
		return nil, errors.New("INVALID_AMOUNT")
	}
	n, ok := new(big.Int).SetString(parts[0]+fraction+strings.Repeat("0", decimals-len(fraction)), 10)
	if !ok || n.Sign() <= 0 {
		return nil, errors.New("INVALID_AMOUNT")
	}
	return n, nil
}

// ParseConfig validates the private service configuration before opening storage.
func ParseConfig(raw string) (Config, error) {
	var c Config
	d := json.NewDecoder(strings.NewReader(raw))
	d.DisallowUnknownFields()
	if d.Decode(&c) != nil {
		return c, errors.New("INVALID_CONFIG")
	}
	return c, c.validate()
}

func (c Config) validate() error {
	id, ok := new(big.Int).SetString(c.ChainID, 10)
	if !ok || id.Sign() <= 0 || id.BitLen() > 64 || !hashPattern.MatchString(c.Genesis) {
		return errors.New("INVALID_NETWORK")
	}
	for _, raw := range []string{c.ReadURL, c.BroadcastURL, c.Origin} {
		u, err := url.Parse(raw)
		if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
			return errors.New("INVALID_ENDPOINT")
		}
	}
	if !regexp.MustCompile(`^[0-9a-f]{64}$`).MatchString(c.ProxyToken) || c.CooldownSeconds < 60 || c.CooldownSeconds > 31536000 || c.IPDailyClaims < 1 || c.IPDailyClaims > 10000 || c.IPRequestsPerMinute < 1 || c.IPRequestsPerMinute > 1000 || c.Confirmations < 1 || c.Confirmations > 100 {
		return errors.New("INVALID_POLICY")
	}
	claim, e1 := Units(c.ClaimAmount, 18)
	budget, e2 := Units(c.DailyBudget, 18)
	_, e3 := Units(c.GasReserve, 18)
	gas, e4 := Units(c.MaxGasPriceGwei, 9)
	if e1 != nil || e2 != nil || e3 != nil || e4 != nil {
		return errors.New("INVALID_AMOUNT")
	}
	if budget.Cmp(new(big.Int).Add(claim, new(big.Int).Mul(gas, big.NewInt(21000)))) < 0 {
		return errors.New("BUDGET_TOO_SMALL")
	}
	return nil
}

func (c Config) reserve() *big.Int {
	amount, _ := Units(c.ClaimAmount, 18)
	gas, _ := Units(c.MaxGasPriceGwei, 9)
	return new(big.Int).Add(amount, new(big.Int).Mul(gas, big.NewInt(21000)))
}
