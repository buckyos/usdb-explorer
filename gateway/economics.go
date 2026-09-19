package main

import (
	"context"
	"errors"
	"math/big"
	"regexp"
	"strconv"
	"strings"
)

const economicsSchema = "usdb-block-economics:v1"

var addressText = regexp.MustCompile(`^0x[0-9a-fA-F]{40}$`)

type economicSelector struct {
	PassID   string `json:"pass_id"`
	Height   uint32 `json:"btc_height"`
	Age      uint32 `json:"btc_anchor_age_blocks"`
	Snapshot string `json:"snapshot_id"`
	System   string `json:"system_state_id"`
	Registry string `json:"activation_registry_id"`
}
type economicAmounts struct {
	Before        string `json:"issued_before_atoms"`
	After         string `json:"issued_after_atoms"`
	Emission      string `json:"emission_atoms"`
	MinerEmission string `json:"miner_emission_atoms"`
	Fees          string `json:"fees_atoms"`
	MinerFees     string `json:"miner_fees_atoms"`
	DAOFees       string `json:"dao_fees_atoms"`
}
type economicTransaction struct {
	Hash   string  `json:"hash"`
	Status *uint64 `json:"status"`
	Gas    string  `json:"gas_used"`
	Price  string  `json:"effective_gas_price_atoms"`
	Fee    string  `json:"fee_atoms"`
	Miner  string  `json:"miner_fee_atoms"`
	DAO    string  `json:"dao_fee_atoms"`
	Route  string  `json:"fee_route"`
}
type blockEconomicReport struct {
	Schema       string                `json:"schema_version"`
	Status       string                `json:"status"`
	Hash         string                `json:"block_hash"`
	Number       string                `json:"block_number"`
	Parent       string                `json:"parent_hash"`
	Root         string                `json:"state_root"`
	Receipts     string                `json:"receipts_root"`
	Miner        string                `json:"miner"`
	Dividend     string                `json:"dividend"`
	Versions     map[string]uint32     `json:"versions,omitempty"`
	Selector     *economicSelector     `json:"selector,omitempty"`
	Amounts      *economicAmounts      `json:"amounts,omitempty"`
	Transactions []economicTransaction `json:"transactions"`
}
type economicsBlock struct {
	Number       string   `json:"number"`
	Hash         string   `json:"hash"`
	Parent       string   `json:"parentHash"`
	Root         string   `json:"stateRoot"`
	Receipts     string   `json:"receiptsRoot"`
	Miner        string   `json:"miner"`
	Gas          string   `json:"gasUsed"`
	Transactions []string `json:"transactions"`
}

func economicsResource(resource string) (string, bool) {
	parts := strings.Split(resource, "/")
	if len(parts) != 3 || parts[0] != "blocks" || parts[2] != "economics" {
		return "", false
	}
	ref := parts[1]
	if chainHash.MatchString(ref) {
		return ref, true
	}
	if !decimalText.MatchString(ref) {
		return "", false
	}
	_, err := strconv.ParseUint(ref, 10, 64)
	return ref, err == nil
}

// atoms validates bounded unsigned decimal amounts without float conversion.
func atoms(value string) *big.Int {
	if len(value) > 78 || !decimalText.MatchString(value) {
		return nil
	}
	n, ok := new(big.Int).SetString(value, 10)
	if !ok || n.BitLen() > 256 {
		return nil
	}
	return n
}
func sumsTo(a, b, c string) bool {
	x, y, z := atoms(a), atoms(b), atoms(c)
	return x != nil && y != nil && z != nil && new(big.Int).Add(x, y).Cmp(z) == 0
}

func (r blockEconomicReport) valid(g *gateway, block economicsBlock) bool {
	number, err := strconv.ParseUint(strings.TrimPrefix(block.Number, "0x"), 16, 64)
	if err != nil || block.Number != "0x"+strconv.FormatUint(number, 16) || r.Number != strconv.FormatUint(number, 10) ||
		r.Schema != economicsSchema || r.Hash != block.Hash || r.Parent != block.Parent || r.Root != block.Root || r.Receipts != block.Receipts ||
		!chainHash.MatchString(r.Hash) || !chainHash.MatchString(r.Parent) || !chainHash.MatchString(r.Root) || !chainHash.MatchString(r.Receipts) ||
		!addressText.MatchString(r.Miner) || !strings.EqualFold(r.Miner, block.Miner) || !addressText.MatchString(r.Dividend) || r.Transactions == nil {
		return false
	}
	if r.Status == "genesis_not_applicable" {
		return number == 0 && r.Amounts == nil && r.Selector == nil && len(r.Transactions) == 0 && len(r.Versions) == 0
	}
	if number == 0 || r.Status != "verified" || r.Selector == nil || r.Amounts == nil || len(r.Transactions) > 2000 ||
		len(r.Transactions) != len(block.Transactions) {
		return false
	}
	s, a, v := r.Selector, r.Amounts, r.Versions
	if !validPassID(s.PassID) || s.Height < g.catalog.BTCOrigin || !digestText.MatchString(s.Snapshot) || !digestText.MatchString(s.System) || s.Registry != g.catalog.Registry ||
		len(v) != 10 || v["payloadVersion"] != 1 || v["rewardRuleVersion"] != 1 || v["coinbaseEmissionPolicyVersion"] != 1 ||
		v["feeSplitPolicyVersion"] > 1 || v["collaborationEfficiencyPolicyVersion"] > 1 || v["pricePolicyVersion"] != 1 || v["auxPoolPolicyVersion"] != 0 ||
		v["quotePolicyVersion"] != 0 || v["btcAnchorPolicyVersion"] != 1 || v["difficultyPolicyVersion"] != 1 {
		return false
	}
	// Reject unknown version keys instead of accidentally presenting a future schema as v1.
	for key := range v {
		switch key {
		case "payloadVersion", "btcAnchorPolicyVersion", "difficultyPolicyVersion", "rewardRuleVersion", "coinbaseEmissionPolicyVersion", "feeSplitPolicyVersion", "collaborationEfficiencyPolicyVersion", "pricePolicyVersion", "quotePolicyVersion", "auxPoolPolicyVersion":
		default:
			return false
		}
	}
	if !sumsTo(a.Before, a.Emission, a.After) || a.Emission != a.MinerEmission || !sumsTo(a.MinerFees, a.DAOFees, a.Fees) {
		return false
	}
	total, miner, dao, gas := new(big.Int), new(big.Int), new(big.Int), new(big.Int)
	for i, tx := range r.Transactions {
		fee, price, used, m, d := atoms(tx.Fee), atoms(tx.Price), atoms(tx.Gas), atoms(tx.Miner), atoms(tx.DAO)
		if !chainHash.MatchString(tx.Hash) || tx.Hash != block.Transactions[i] || tx.Status == nil || *tx.Status > 1 || fee == nil || price == nil || used == nil || m == nil || d == nil ||
			new(big.Int).Mul(used, price).Cmp(fee) != 0 || !sumsTo(tx.Miner, tx.DAO, tx.Fee) {
			return false
		}
		if tx.Route != "miner_only" && tx.Route != "miner_and_dividend" {
			return false
		}
		if tx.Route == "miner_only" && d.Sign() != 0 {
			return false
		}
		if tx.Route == "miner_and_dividend" && (v["feeSplitPolicyVersion"] != 1 || r.Dividend == "0x"+strings.Repeat("0", 40)) {
			return false
		}
		total.Add(total, fee)
		miner.Add(miner, m)
		dao.Add(dao, d)
		gas.Add(gas, used)
	}
	return total.String() == a.Fees && miner.String() == a.MinerFees && dao.String() == a.DAOFees && block.Gas == "0x"+gas.Text(16)
}

func (g *gateway) blockEconomics(ctx context.Context, ref string) (*blockEconomicReport, error) {
	method, query := "eth_getBlockByHash", ref
	if !chainHash.MatchString(ref) {
		n, _ := strconv.ParseUint(ref, 10, 64)
		method, query = "eth_getBlockByNumber", "0x"+strconv.FormatUint(n, 16)
	}
	var block *economicsBlock
	if err := g.readRPCValue(ctx, method, []any{query, false}, &block, true); err != nil {
		return nil, err
	}
	if block == nil {
		return nil, &publicFailure{404, "BLOCK_NOT_FOUND"}
	}
	if !chainHash.MatchString(block.Hash) || (method == "eth_getBlockByHash" && block.Hash != ref) || (method == "eth_getBlockByNumber" && block.Number != query) {
		return nil, invalidResponse()
	}
	var report blockEconomicReport
	if err := g.rpcValue(ctx, "eth_getUSDBBlockEconomics", []any{block.Hash}, &report); err != nil {
		var failure *publicFailure
		if errors.As(err, &failure) && failure.code == "INDEXER_INCOMPATIBLE" {
			return nil, &publicFailure{503, "ECONOMICS_NODE_UPGRADE_REQUIRED"}
		}
		return nil, err
	}
	if !report.valid(g, *block) {
		return nil, invalidResponse()
	}
	var canonical struct {
		Hash string `json:"hash"`
	}
	if err := g.rpcValue(ctx, "eth_getBlockByNumber", []any{block.Number, false}, &canonical); err != nil {
		return nil, err
	}
	if canonical.Hash != block.Hash {
		return nil, &publicFailure{409, "BLOCK_NOT_CANONICAL"}
	}
	return &report, nil
}
