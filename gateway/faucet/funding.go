package faucet

import (
	"context"
	"database/sql"
	"errors"
	"strings"
)

const activeExternalQuery = "SELECT " + jobColumns + " FROM jobs WHERE kind IN ('fund','miner_transfer') AND status NOT IN ('confirmed','failed')"

// RefreshFunding observes saved refills and miner transfers without signing or rebroadcasting.
// Explicit retry remains available when an operator needs to resend journaled bytes.
func (s *Service) RefreshFunding(ctx context.Context) error {
	return s.refreshFunding(ctx, "")
}

func (s *Service) refreshFunding(ctx context.Context, sender string) error {
	query := activeExternalQuery
	args := []any{}
	if sender != "" {
		query += " AND sender=?"
		args = append(args, sender)
	}
	rows, err := s.db.QueryContext(ctx, query+" ORDER BY created,id", args...)
	if err != nil {
		return errors.New("STORAGE_UNAVAILABLE")
	}
	jobs := []Job{}
	for rows.Next() {
		j, scanErr := scanJob(rows)
		if scanErr != nil {
			rows.Close()
			return errors.New("STORAGE_UNAVAILABLE")
		}
		jobs = append(jobs, j)
	}
	err = rows.Err()
	rows.Close() // Release the single DB connection before any status writes or RPC calls.
	if err != nil {
		return errors.New("STORAGE_UNAVAILABLE")
	}
	if len(jobs) == 0 {
		return nil
	}
	if err := identity(ctx, s.rpc, s.config); err != nil {
		return err
	}
	for _, j := range jobs {
		state, err := receiptState(ctx, s.rpc, j.Hash, s.config.Confirmations)
		if err != nil {
			return err
		}
		// A concurrent explicit retry may already have settled this record.
		// An older observation must not reopen it after another refill has started.
		if _, err = s.db.ExecContext(ctx, "UPDATE jobs SET status=?,day=CASE WHEN ? IN ('confirmed','failed') THEN ? ELSE day END WHERE id=? AND status NOT IN ('confirmed','failed')", state, state, s.now().UTC().Format("2006-01-02"), j.ID); err != nil {
			return errors.New("STORAGE_UNAVAILABLE")
		}
	}
	return nil
}

// fundingBlocker returns the existing public recovery receipt instead of an empty job.
func fundingBlocker(row scanner) (Job, error) {
	j, err := scanJob(row)
	if errors.Is(err, sql.ErrNoRows) {
		return Job{}, nil
	}
	if err != nil {
		return Job{}, errors.New("STORAGE_UNAVAILABLE")
	}
	code := "FUNDING_PENDING"
	if j.Kind == "miner_transfer" {
		code = "TRANSFER_PENDING"
	}
	prefix := "f_"
	if j.Kind == "miner_transfer" {
		prefix = "t_"
	}
	j.ID = strings.TrimPrefix(j.ID, prefix)
	return j, errors.New(code)
}
