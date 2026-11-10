package faucet

import (
	"context"
	"database/sql"
	"encoding/hex"
	"errors"
	"math/big"
	"strings"

	"github.com/ethereum/go-ethereum/crypto"
)

// OperatorReceipt identifies which private command and account own a recovery ID.
// Keys, signed bytes and internal sender metadata stay in the ledger.
type OperatorReceipt struct {
	Job
	Operation string `json:"operation"`
	Source    string `json:"source"`
}

// DescribeJob adds operator-only context without widening the public claim API.
func DescribeJob(j Job) OperatorReceipt {
	operation, source := "transfer", "faucet"
	switch j.Kind {
	case "fund":
		operation, source = "fund", "miner"
	case "miner_transfer":
		source = "miner"
	case "faucet_transfer":
	case "claim":
		operation = "claim"
	}
	return OperatorReceipt{Job: j, Operation: operation, Source: source}
}

// Transfer submits an explicit operator payment. Faucet payments are queued for
// the single wallet worker; miner payments use only the supplied one-shot key.
func (s *Service) Transfer(ctx context.Context, source, id, recipient, amountText, keyText string) (Job, error) {
	switch source {
	case "miner":
		return s.externalTransfer(ctx, "miner_transfer", id, recipient, amountText, keyText)
	case "faucet":
		if keyText != "" {
			return Job{}, errors.New("INVALID_PRIVATE_KEY")
		}
		return s.queueTransfer(ctx, id, recipient, amountText)
	default:
		return Job{}, errors.New("INVALID_SOURCE")
	}
}

func (s *Service) externalTransfer(ctx context.Context, kind, id, recipient, amountText, keyText string) (Job, error) {
	if !fundingID.MatchString(id) {
		return Job{}, errors.New("INVALID_REQUEST_ID")
	}
	amount, err := Units(amountText, 18)
	if err != nil {
		return Job{}, err
	}
	to, err := address(recipient)
	if err != nil {
		return Job{}, err
	}
	key, err := crypto.HexToECDSA(strings.TrimPrefix(strings.TrimSpace(keyText), "0x"))
	if err != nil {
		return Job{}, errors.New("INVALID_PRIVATE_KEY")
	}
	defer key.D.SetInt64(0)
	sender := crypto.PubkeyToAddress(key.PublicKey).Hex()
	if sender == s.sender() {
		return Job{}, errors.New("FUNDING_REQUIRES_DIFFERENT_ACCOUNT")
	}
	if sender == to.Hex() {
		return Job{}, errors.New("INVALID_ADDRESS")
	}
	prefix := "t_"
	if kind == "fund" {
		prefix = "f_"
	}
	storedID := prefix + id
	if old, e := s.job(storedID); e == nil {
		if old.Kind != kind || old.Sender != sender || old.Address != to.Hex() || old.Amount != amount.String() {
			return Job{}, errors.New("REQUEST_ID_CONFLICT")
		}
		return s.retryExternal(ctx, kind, id)
	} else if !errors.Is(e, sql.ErrNoRows) {
		return Job{}, errors.New("STORAGE_UNAVAILABLE")
	}
	// Refills and directed miner transfers share the same nonce owner and gate.
	if err := s.refreshFunding(ctx, sender); err != nil {
		return Job{}, err
	}
	if pending, err := fundingBlocker(s.db.QueryRowContext(ctx, activeExternalQuery+" AND sender=? LIMIT 1", sender)); err != nil {
		return pending, err
	}
	signed, err := signedTransfer(ctx, s.rpc, s.config, key, to, amount)
	if err != nil {
		return Job{}, err
	}
	raw, err := signed.MarshalBinary()
	if err != nil {
		return Job{}, errors.New("SIGNING_FAILED")
	}
	j := Job{ID: storedID, Kind: kind, Address: to.Hex(), Sender: sender, Amount: amount.String(), Reserved: "0", Day: s.now().UTC().Format("2006-01-02"), Created: s.now().Unix(), Status: "pending", Raw: "0x" + hex.EncodeToString(raw), Hash: signed.Hash().Hex(), Nonce: sql.NullString{String: nonceText(signed.Nonce()), Valid: true}}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return Job{}, errors.New("STORAGE_UNAVAILABLE")
	}
	defer tx.Rollback()
	if pending, err := fundingBlocker(tx.QueryRowContext(ctx, activeExternalQuery+" AND sender=? LIMIT 1", sender)); err != nil {
		return pending, err
	}
	if err := insertJob(tx, j); err != nil {
		return Job{}, errors.New("NONCE_OR_REQUEST_CONFLICT")
	}
	if err := tx.Commit(); err != nil {
		return Job{}, errors.New("STORAGE_UNAVAILABLE")
	}
	return s.retryExternal(ctx, kind, id)
}

func (s *Service) retryExternal(ctx context.Context, kind, id string) (Job, error) {
	if !fundingID.MatchString(id) {
		return Job{}, errors.New("INVALID_REQUEST_ID")
	}
	prefix, missing := "t_", "TRANSFER_NOT_FOUND"
	if kind == "fund" {
		prefix, missing = "f_", "FUNDING_NOT_FOUND"
	}
	j, err := s.job(prefix + id)
	if err != nil || j.Kind != kind {
		return Job{}, errors.New(missing)
	}
	if err = identity(ctx, s.rpc, s.config); err != nil {
		j.ID = id
		return j, err
	}
	err = s.resume(ctx, j)
	if current, e := s.job(j.ID); e == nil {
		j = current
	}
	j.ID = id
	return j, err
}

// RetryTransfer resumes a miner transaction or reads the existing faucet queue
// entry. Only the worker can sign or retry a faucet-wallet transaction.
func (s *Service) RetryTransfer(ctx context.Context, id string) (Job, error) {
	if !fundingID.MatchString(id) {
		return Job{}, errors.New("INVALID_REQUEST_ID")
	}
	j, err := s.job("t_" + id)
	if err != nil {
		return Job{}, errors.New("TRANSFER_NOT_FOUND")
	}
	if j.Kind == "miner_transfer" {
		return s.retryExternal(ctx, j.Kind, id)
	}
	if j.Kind != "faucet_transfer" {
		return Job{}, errors.New("TRANSFER_NOT_FOUND")
	}
	j.ID = id
	return j, nil
}

func queuedTransfer(row scanner, recipient, amount, sender string) (Job, error) {
	old, err := scanJob(row)
	if err != nil {
		return Job{}, err
	}
	if old.Kind != "faucet_transfer" || old.Address != recipient || old.Amount != amount || old.Sender != sender {
		return Job{}, errors.New("REQUEST_ID_CONFLICT")
	}
	old.ID = strings.TrimPrefix(old.ID, "t_")
	return old, nil
}

func (s *Service) queueTransfer(ctx context.Context, id, recipient, amountText string) (Job, error) {
	if !fundingID.MatchString(id) {
		return Job{}, errors.New("INVALID_REQUEST_ID")
	}
	to, err := address(recipient)
	if err != nil {
		return Job{}, err
	}
	if to.Hex() == s.sender() {
		return Job{}, errors.New("INVALID_ADDRESS")
	}
	amount, err := Units(amountText, 18)
	if err != nil {
		return Job{}, err
	}
	query := "SELECT " + jobColumns + " FROM jobs WHERE id=?"
	storedID := "t_" + id
	if old, err := queuedTransfer(s.db.QueryRowContext(ctx, query, storedID), to.Hex(), amount.String(), s.sender()); !errors.Is(err, sql.ErrNoRows) {
		return old, err
	}
	if _, err := transferGas(ctx, s.rpc, s.config, to); err != nil {
		return Job{}, err
	}
	balance, err := number(ctx, s.rpc, "eth_getBalance", s.sender(), "latest")
	if err != nil {
		return Job{}, err
	}
	gas, _ := Units(s.config.MaxGasPriceGwei, 9)
	reservation := new(big.Int).Add(amount, new(big.Int).Mul(gas, big.NewInt(21000)))
	gasReserve, _ := Units(s.config.GasReserve, 18)
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return Job{}, errors.New("STORAGE_UNAVAILABLE")
	}
	defer tx.Rollback()
	if old, err := queuedTransfer(tx.QueryRowContext(ctx, query, storedID), to.Hex(), amount.String(), s.sender()); !errors.Is(err, sql.ErrNoRows) {
		return old, err
	}
	// Protect funds already promised to queued claims or operator payments.
	// Counting confirming payments too is conservative until their receipts settle.
	rows, err := tx.QueryContext(ctx, "SELECT reserved FROM jobs WHERE kind IN ('claim','faucet_transfer') AND status IN ('queued','pending','confirming')")
	if err != nil {
		return Job{}, errors.New("STORAGE_UNAVAILABLE")
	}
	outstanding := new(big.Int)
	count := 0
	for rows.Next() {
		var raw string
		if err := rows.Scan(&raw); err != nil {
			rows.Close()
			return Job{}, errors.New("STORAGE_UNAVAILABLE")
		}
		value, ok := new(big.Int).SetString(raw, 10)
		if !ok || value.Sign() < 0 {
			rows.Close()
			return Job{}, errors.New("LEDGER_INVALID")
		}
		outstanding.Add(outstanding, value)
		count++
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return Job{}, errors.New("STORAGE_UNAVAILABLE")
	}
	if count >= 100 {
		return Job{}, errors.New("QUEUE_FULL")
	}
	required := new(big.Int).Add(outstanding, reservation)
	required.Add(required, gasReserve)
	if balance.Cmp(required) < 0 {
		return Job{}, errors.New("INSUFFICIENT_FUNDS")
	}
	j := Job{ID: storedID, Kind: "faucet_transfer", Address: to.Hex(), Sender: s.sender(), Amount: amount.String(), Reserved: reservation.String(), Day: s.now().UTC().Format("2006-01-02"), Created: s.now().Unix(), Status: "queued"}
	if err := insertJob(tx, j); err != nil {
		return Job{}, errors.New("STORAGE_UNAVAILABLE")
	}
	if err := tx.Commit(); err != nil {
		return Job{}, errors.New("STORAGE_UNAVAILABLE")
	}
	j.ID = id
	return j, nil
}
