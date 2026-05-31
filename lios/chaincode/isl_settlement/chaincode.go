package main

import (
	"encoding/json"
	"fmt"
	"time"

	"github.com/hyperledger/fabric-contract-api-go/contractapi"
)

// ISLSettlementChaincode implements all LIOS on-chain functions.
type ISLSettlementChaincode struct {
	contractapi.Contract
}

// ── Internal helpers ──────────────────────────────────────────────────────────

func nowSec(ctx contractapi.TransactionContextInterface) int64 {
	ts, err := ctx.GetStub().GetTxTimestamp()
	if err != nil {
		return time.Now().Unix()
	}
	return ts.Seconds
}

func nowMs(ctx contractapi.TransactionContextInterface) int64 {
	return nowSec(ctx) * 1000
}

func emit(ctx contractapi.TransactionContextInterface, name string, payload interface{}) {
	b, _ := json.Marshal(payload)
	_ = ctx.GetStub().SetEvent(name, b)
}

func putState(ctx contractapi.TransactionContextInterface, key string, obj interface{}) error {
	b, err := json.Marshal(obj)
	if err != nil {
		return err
	}
	return ctx.GetStub().PutState(key, b)
}

func getOperatorChannel(ctx contractapi.TransactionContextInterface, id string) (*OperatorChannel, error) {
	raw, err := ctx.GetStub().GetState("OCH:" + id)
	if err != nil || raw == nil {
		return nil, fmt.Errorf("operator channel %q not found", id)
	}
	var ch OperatorChannel
	if err := json.Unmarshal(raw, &ch); err != nil {
		return nil, err
	}
	return &ch, nil
}

// getSettlementRecord returns nil, nil when the record does not exist yet.
func getSettlementRecord(ctx contractapi.TransactionContextInterface, satPairID string) (*SettlementRecord, error) {
	raw, err := ctx.GetStub().GetState("SR:" + satPairID)
	if err != nil {
		return nil, err
	}
	if raw == nil {
		return nil, nil
	}
	var r SettlementRecord
	if err := json.Unmarshal(raw, &r); err != nil {
		return nil, err
	}
	return &r, nil
}

// getDisputeRecord returns nil, nil when no dispute is pending for the pair.
func getDisputeRecord(ctx contractapi.TransactionContextInterface, satPairID string) (*DisputeRecord, error) {
	raw, err := ctx.GetStub().GetState("DR:" + satPairID)
	if err != nil {
		return nil, err
	}
	if raw == nil {
		return nil, nil
	}
	var r DisputeRecord
	if err := json.Unmarshal(raw, &r); err != nil {
		return nil, err
	}
	return &r, nil
}

func getSatKey(ctx contractapi.TransactionContextInterface, satID string) (*SatKey, error) {
	raw, err := ctx.GetStub().GetState("SAT:" + satID)
	if err != nil || raw == nil {
		return nil, fmt.Errorf("satellite key for %q not found", satID)
	}
	var k SatKey
	if err := json.Unmarshal(raw, &k); err != nil {
		return nil, err
	}
	return &k, nil
}

// ensureSettlementRecord returns the existing record or creates a new one.
func ensureSettlementRecord(
	ctx contractapi.TransactionContextInterface,
	satPairID, opChannelID string,
) (*SettlementRecord, error) {
	sr, err := getSettlementRecord(ctx, satPairID)
	if err != nil {
		return nil, err
	}
	if sr != nil {
		return sr, nil
	}
	satA, satB, err := parseSatPairID(satPairID)
	if err != nil {
		return nil, err
	}
	sr = &SettlementRecord{
		SatPairID:         satPairID,
		SatA:              satA,
		SatB:              satB,
		OperatorChannelID: opChannelID,
		OutOfServiceLog:   []OutOfServiceEntry{},
	}
	return sr, nil
}

// ── Operator Channel Lifecycle ────────────────────────────────────────────────

// EnsureOperatorChannel idempotently opens a bilateral operator channel.
// If the channel already exists the call is a no-op (returns nil).
func (c *ISLSettlementChaincode) EnsureOperatorChannel(
	ctx contractapi.TransactionContextInterface,
	operatorA, operatorB string,
	balanceA, balanceB, reserveA, reserveB float64,
) error {
	chID := operatorA + "_" + operatorB + "_ch"
	if existing, _ := ctx.GetStub().GetState("OCH:" + chID); existing != nil {
		return nil // idempotent
	}
	ch := OperatorChannel{
		ID:              chID,
		OperatorA:       operatorA,
		OperatorB:       operatorB,
		BalanceA:        balanceA,
		BalanceB:        balanceB,
		PenaltyReserveA: reserveA,
		PenaltyReserveB: reserveB,
		Status:          "OPEN",
		OpenedAt:        nowMs(ctx),
	}
	if err := putState(ctx, "OCH:"+chID, ch); err != nil {
		return err
	}
	emit(ctx, EventChannelOpened, map[string]string{"channelId": chID})
	return nil
}

// InitLedger bulk-registers satellite keys and opens operator channels in one
// atomic transaction.  Replaces issuing a separate UpsertSatelliteKey invoke per
// satellite, which serialises through block ordering and is prohibitively slow for
// large constellations.  Both arrays are idempotent: existing records are skipped.
func (c *ISLSettlementChaincode) InitLedger(
	ctx contractapi.TransactionContextInterface,
	operatorsJSON string,
	satellitesJSON string,
) error {
	var operators []OperatorChannelInit
	if err := json.Unmarshal([]byte(operatorsJSON), &operators); err != nil {
		return fmt.Errorf("invalid operators JSON: %w", err)
	}
	var satellites []SatKeyInit
	if err := json.Unmarshal([]byte(satellitesJSON), &satellites); err != nil {
		return fmt.Errorf("invalid satellites JSON: %w", err)
	}

	now := nowMs(ctx)

	for _, op := range operators {
		chID := op.OperatorA + "_" + op.OperatorB + "_ch"
		if existing, _ := ctx.GetStub().GetState("OCH:" + chID); existing != nil {
			continue // idempotent
		}
		ch := OperatorChannel{
			ID:              chID,
			OperatorA:       op.OperatorA,
			OperatorB:       op.OperatorB,
			BalanceA:        op.BalanceA,
			BalanceB:        op.BalanceB,
			PenaltyReserveA: op.ReserveA,
			PenaltyReserveB: op.ReserveB,
			Status:          "OPEN",
			OpenedAt:        now,
		}
		if err := putState(ctx, "OCH:"+chID, ch); err != nil {
			return err
		}
		emit(ctx, EventChannelOpened, map[string]string{"channelId": chID})
	}

	for _, sat := range satellites {
		// Always overwrite satellite keys — each simulation run generates fresh key
		// pairs, so stale keys from a previous run must be replaced or signatures
		// will fail verification.
		key := SatKey{
			SatelliteID:  sat.SatelliteID,
			OperatorID:   sat.OperatorID,
			PubKeyPEM:    sat.PubKeyPEM,
			IsRevoked:    false,
			RegisteredAt: now,
		}
		if err := putState(ctx, "SAT:"+sat.SatelliteID, key); err != nil {
			return err
		}
	}

	emit(ctx, EventKeyRegistered, map[string]interface{}{
		"bulk":       true,
		"operators":  len(operators),
		"satellites": len(satellites),
	})
	return nil
}

// ── Satellite Key Management ──────────────────────────────────────────────────

// UpsertSatelliteKey registers or overwrites a satellite public key on-chain.
// The AND endorsement policy from all operator peers serves as the authority check.
func (c *ISLSettlementChaincode) UpsertSatelliteKey(
	ctx contractapi.TransactionContextInterface,
	operatorID, satelliteID, pubKeyPEM string,
) error {
	key := SatKey{
		SatelliteID:  satelliteID,
		OperatorID:   operatorID,
		PubKeyPEM:    pubKeyPEM,
		IsRevoked:    false,
		RegisteredAt: nowMs(ctx),
	}
	if err := putState(ctx, "SAT:"+satelliteID, key); err != nil {
		return err
	}
	emit(ctx, EventKeyRegistered, map[string]string{
		"satelliteId": satelliteID, "operatorId": operatorID,
	})
	return nil
}

// GetSatelliteKey returns the on-chain key record for a satellite.
func (c *ISLSettlementChaincode) GetSatelliteKey(
	ctx contractapi.TransactionContextInterface,
	satelliteID string,
) (*SatKey, error) {
	return getSatKey(ctx, satelliteID)
}

// RevokeSatelliteKey marks a satellite key as revoked.
func (c *ISLSettlementChaincode) RevokeSatelliteKey(
	ctx contractapi.TransactionContextInterface,
	operatorID, satelliteID string,
) error {
	key, err := getSatKey(ctx, satelliteID)
	if err != nil {
		return err
	}
	msp, _ := ctx.GetClientIdentity().GetMSPID()
	if msp != operatorID+"MSP" {
		return fmt.Errorf("caller %q cannot revoke key owned by %s", msp, operatorID)
	}
	now := nowMs(ctx)
	key.IsRevoked = true
	key.RevokedAt = now
	if err := putState(ctx, "SAT:"+satelliteID, key); err != nil {
		return err
	}
	emit(ctx, EventKeyRevoked, map[string]string{
		"satelliteId": satelliteID, "operatorId": operatorID,
	})
	return nil
}

// ── Settlement ────────────────────────────────────────────────────────────────

// SubmitCoSignedSettlement finalises a bilateral settlement immediately.
// Both sigA and sigB must be present and valid.  The SettlementRecord is
// created lazily on first call; no prior channel registration is required.
// Satellite public keys are resolved from the on-chain SatKey registry.
func (c *ISLSettlementChaincode) SubmitCoSignedSettlement(
	ctx contractapi.TransactionContextInterface,
	satPairID string,
	opChannelID string,
	proofJSON string,
) error {
	var proof BalanceProof
	if err := json.Unmarshal([]byte(proofJSON), &proof); err != nil {
		return fmt.Errorf("invalid proof JSON: %w", err)
	}
	if proof.SigA == "" || proof.SigB == "" {
		return fmt.Errorf("co-signed settlement requires both sigA and sigB")
	}

	sr, err := ensureSettlementRecord(ctx, satPairID, opChannelID)
	if err != nil {
		return err
	}
	if proof.SeqNum == sr.LatestSeqNum {
		return nil // already settled at this seq_num — second GS submission is idempotent
	}
	if proof.SeqNum < sr.LatestSeqNum {
		return fmt.Errorf("proof seqNum %d < latest settled seqNum %d (rollback attempt)", proof.SeqNum, sr.LatestSeqNum)
	}

	keyA, err := getSatKey(ctx, sr.SatA)
	if err != nil {
		return fmt.Errorf("no key for %s: %w", sr.SatA, err)
	}
	keyB, err := getSatKey(ctx, sr.SatB)
	if err != nil {
		return fmt.Errorf("no key for %s: %w", sr.SatB, err)
	}
	if err := VerifyBalanceProof(proof, keyA.PubKeyPEM, keyB.PubKeyPEM); err != nil {
		return fmt.Errorf("signature verification failed: %w", err)
	}

	msp, _ := ctx.GetClientIdentity().GetMSPID()
	now := nowMs(ctx)
	proof.SubmittedBy = msp
	proof.SubmittedAt = now

	sr.LatestSeqNum = proof.SeqNum
	sr.LastUpdated = now

	if err := putState(ctx, "SR:"+satPairID, sr); err != nil {
		return err
	}
	emit(ctx, EventSettlementFinalized, map[string]interface{}{
		"satPairId": satPairID,
		"seqNum":    sr.LatestSeqNum,
	})
	return nil
}

// SubmitBalanceReset settles the current session AND resets per-satellite balances
// to the equal-split starting point, debiting the net transfer from the operator
// channel.  Both sigA and sigB are mandatory: the co-signed BalanceProof is the
// dual authorisation proving both satellites agreed on the final state.
//
// Net debit rule: whichever satellite forwarded more has a lower final balance.
// Let half = (balA + balB) / 2.  If balA < half, satellite A forwarded more →
// operator B pays operator A: opCh.BalanceB -= delta, opCh.BalanceA += delta
// (where delta = half - balA > 0).  The sign flips when balB < half.
func (c *ISLSettlementChaincode) SubmitBalanceReset(
	ctx contractapi.TransactionContextInterface,
	satPairID string,
	opChannelID string,
	proofJSON string,
) error {
	var proof BalanceProof
	if err := json.Unmarshal([]byte(proofJSON), &proof); err != nil {
		return fmt.Errorf("invalid proof JSON: %w", err)
	}
	if proof.SigA == "" || proof.SigB == "" {
		return fmt.Errorf("balance reset requires co-signed proof (both sigA and sigB)")
	}

	sr, err := ensureSettlementRecord(ctx, satPairID, opChannelID)
	if err != nil {
		return err
	}
	if proof.SeqNum == sr.LatestSeqNum {
		return nil // already reset at this seq_num — second GS submission is idempotent
	}
	if proof.SeqNum < sr.LatestSeqNum {
		return fmt.Errorf("proof seqNum %d < latest settled seqNum %d (rollback attempt)", proof.SeqNum, sr.LatestSeqNum)
	}

	keyA, err := getSatKey(ctx, sr.SatA)
	if err != nil {
		return fmt.Errorf("no key for %s: %w", sr.SatA, err)
	}
	keyB, err := getSatKey(ctx, sr.SatB)
	if err != nil {
		return fmt.Errorf("no key for %s: %w", sr.SatB, err)
	}
	if err := VerifyBalanceProof(proof, keyA.PubKeyPEM, keyB.PubKeyPEM); err != nil {
		return fmt.Errorf("signature verification failed: %w", err)
	}

	// Net transfer accounting: half is the equal-split reset target.
	half := (proof.BalanceA + proof.BalanceB) / 2.0
	delta := half - proof.BalanceA // positive → A forwarded more → B pays A

	opCh, err := getOperatorChannel(ctx, opChannelID)
	if err != nil {
		return err
	}
	if delta > 0 && opCh.BalanceB < delta {
		return fmt.Errorf("operator B has insufficient balance for reset (need %.4f, have %.4f)", delta, opCh.BalanceB)
	}
	if delta < 0 && opCh.BalanceA < -delta {
		return fmt.Errorf("operator A has insufficient balance for reset (need %.4f, have %.4f)", -delta, opCh.BalanceA)
	}
	opCh.BalanceA += delta
	opCh.BalanceB -= delta
	if err := putState(ctx, "OCH:"+opChannelID, opCh); err != nil {
		return err
	}

	msp, _ := ctx.GetClientIdentity().GetMSPID()
	now := nowMs(ctx)
	proof.SubmittedBy = msp
	proof.SubmittedAt = now

	sr.LatestSeqNum = proof.SeqNum
	sr.LastUpdated = now
	if err := putState(ctx, "SR:"+satPairID, sr); err != nil {
		return err
	}
	emit(ctx, EventBalanceReset, map[string]interface{}{
		"satPairId":    satPairID,
		"seqNum":       proof.SeqNum,
		"delta":        delta,
		"opChannelId":  opChannelID,
	})
	return nil
}

// InitiateSettlement submits a unilateral BalanceProof and opens a T_challenge
// window by creating an ephemeral DisputeRecord.  Used when the counterpart GS
// is unreachable.  At least one signature (sigA or sigB) must be valid.
func (c *ISLSettlementChaincode) InitiateSettlement(
	ctx contractapi.TransactionContextInterface,
	satPairID string,
	opChannelID string,
	proofJSON string,
) error {
	// Reject if a dispute is already pending.
	existing, err := getDisputeRecord(ctx, satPairID)
	if err != nil {
		return err
	}
	if existing != nil {
		return fmt.Errorf("settlement already pending for pair %q", satPairID)
	}

	var proof BalanceProof
	if err := json.Unmarshal([]byte(proofJSON), &proof); err != nil {
		return fmt.Errorf("invalid proof JSON: %w", err)
	}

	sr, err := ensureSettlementRecord(ctx, satPairID, opChannelID)
	if err != nil {
		return err
	}
	if proof.SeqNum <= sr.LatestSeqNum {
		return fmt.Errorf("proof seqNum %d <= latest settled seqNum %d", proof.SeqNum, sr.LatestSeqNum)
	}

	keyA, err := getSatKey(ctx, sr.SatA)
	if err != nil {
		return fmt.Errorf("no key for %s: %w", sr.SatA, err)
	}
	keyB, err := getSatKey(ctx, sr.SatB)
	if err != nil {
		return fmt.Errorf("no key for %s: %w", sr.SatB, err)
	}
	if err := VerifyBalanceProof(proof, keyA.PubKeyPEM, keyB.PubKeyPEM); err != nil {
		return fmt.Errorf("signature verification failed: %w", err)
	}

	msp, _ := ctx.GetClientIdentity().GetMSPID()
	now := nowMs(ctx)
	proof.SubmittedBy = msp
	proof.SubmittedAt = now

	dr := DisputeRecord{
		SatPairID:         satPairID,
		SatA:              sr.SatA,
		SatB:              sr.SatB,
		OperatorChannelID: opChannelID,
		SubmittedProof:    &proof,
		SubmittedAt:       now,
	}
	if err := putState(ctx, "DR:"+satPairID, dr); err != nil {
		return err
	}
	// Persist updated SettlementRecord (created lazily above).
	sr.LastUpdated = now
	if err := putState(ctx, "SR:"+satPairID, sr); err != nil {
		return err
	}
	emit(ctx, EventSettlementInitiated, map[string]interface{}{
		"satPairId":   satPairID,
		"seqNum":      proof.SeqNum,
		"submittedBy": msp,
	})
	return nil
}

// ChallengeSettlement submits a newer BalanceProof to challenge a pending
// unilateral settlement.  Slashes the initiating party's penalty reserve.
// If the counter proof is co-signed, settlement is finalised immediately;
// otherwise the DisputeRecord is updated and the window continues.
func (c *ISLSettlementChaincode) ChallengeSettlement(
	ctx contractapi.TransactionContextInterface,
	satPairID string,
	counterProofJSON string,
) error {
	dr, err := getDisputeRecord(ctx, satPairID)
	if err != nil {
		return err
	}
	if dr == nil {
		return fmt.Errorf("no pending dispute for pair %q", satPairID)
	}
	if dr.SubmittedAt == 0 {
		return fmt.Errorf("no settlement timestamp recorded for pair %q", satPairID)
	}
	deadline := dr.SubmittedAt/1000 + TChallengeWindowSec
	if nowSec(ctx) > deadline {
		return fmt.Errorf("challenge window expired at unix %d", deadline)
	}

	var counter BalanceProof
	if err := json.Unmarshal([]byte(counterProofJSON), &counter); err != nil {
		return fmt.Errorf("invalid counter proof JSON: %w", err)
	}
	if dr.SubmittedProof == nil || counter.SeqNum <= dr.SubmittedProof.SeqNum {
		var have int64
		if dr.SubmittedProof != nil {
			have = dr.SubmittedProof.SeqNum
		}
		return fmt.Errorf("counter proof seqNum %d is not newer than submitted %d", counter.SeqNum, have)
	}

	keyA, err := getSatKey(ctx, dr.SatA)
	if err != nil {
		return fmt.Errorf("no key for %s: %w", dr.SatA, err)
	}
	keyB, err := getSatKey(ctx, dr.SatB)
	if err != nil {
		return fmt.Errorf("no key for %s: %w", dr.SatB, err)
	}
	if err := VerifyBalanceProof(counter, keyA.PubKeyPEM, keyB.PubKeyPEM); err != nil {
		return fmt.Errorf("counter proof verification failed: %w", err)
	}

	// Slash penalty reserve from the initiating (dishonest) party.
	opCh, opErr := getOperatorChannel(ctx, dr.OperatorChannelID)
	if opErr == nil && dr.SubmittedProof != nil {
		if dr.SubmittedProof.SubmittedBy == opCh.OperatorA+"MSP" {
			opCh.PenaltyReserveB += opCh.PenaltyReserveA
			opCh.PenaltyReserveA = 0
		} else {
			opCh.PenaltyReserveA += opCh.PenaltyReserveB
			opCh.PenaltyReserveB = 0
		}
		_ = putState(ctx, "OCH:"+dr.OperatorChannelID, opCh)
	}

	now := nowMs(ctx)
	dr.SubmittedProof = &counter

	emit(ctx, EventSettlementChallenged, map[string]interface{}{
		"satPairId": satPairID, "counterSeqNum": counter.SeqNum,
	})
	emit(ctx, EventPenaltyApplied, map[string]interface{}{"satPairId": satPairID})

	// If counter proof is co-signed, finalise immediately.
	if counter.SigA != "" && counter.SigB != "" {
		sr, err := getSettlementRecord(ctx, satPairID)
		if err != nil {
			return err
		}
		if sr == nil {
			sr = &SettlementRecord{
				SatPairID:         satPairID,
				SatA:              dr.SatA,
				SatB:              dr.SatB,
				OperatorChannelID: dr.OperatorChannelID,
				OutOfServiceLog:   []OutOfServiceEntry{},
			}
		}
		sr.LatestSeqNum = counter.SeqNum
		sr.LastUpdated = now
		if err := putState(ctx, "SR:"+satPairID, sr); err != nil {
			return err
		}
		// Delete the resolved dispute record.
		if err := ctx.GetStub().DelState("DR:" + satPairID); err != nil {
			return err
		}
		emit(ctx, EventSettlementFinalized, map[string]interface{}{
			"satPairId": satPairID, "seqNum": counter.SeqNum,
		})
		return nil
	}

	// Single-sig challenge: update dispute record and wait for FinalizeSettlement.
	return putState(ctx, "DR:"+satPairID, dr)
}

// FinalizeSettlement applies the accepted proof after the T_challenge window
// expires with no successful challenge (or after a single-sig challenge).
// Deletes the DisputeRecord and updates the SettlementRecord.
func (c *ISLSettlementChaincode) FinalizeSettlement(
	ctx contractapi.TransactionContextInterface,
	satPairID string,
) error {
	dr, err := getDisputeRecord(ctx, satPairID)
	if err != nil {
		return err
	}
	if dr == nil {
		return fmt.Errorf("no pending dispute for pair %q", satPairID)
	}
	if dr.SubmittedAt == 0 {
		return fmt.Errorf("no settlement timestamp for pair %q", satPairID)
	}
	deadline := dr.SubmittedAt/1000 + TChallengeWindowSec
	if nowSec(ctx) < deadline {
		return fmt.Errorf("T_challenge has not expired yet (deadline unix: %d)", deadline)
	}
	if dr.SubmittedProof == nil {
		return fmt.Errorf("no submitted proof for pair %q", satPairID)
	}

	now := nowMs(ctx)
	sr, err := getSettlementRecord(ctx, satPairID)
	if err != nil {
		return err
	}
	if sr == nil {
		sr = &SettlementRecord{
			SatPairID:         satPairID,
			SatA:              dr.SatA,
			SatB:              dr.SatB,
			OperatorChannelID: dr.OperatorChannelID,
			OutOfServiceLog:   []OutOfServiceEntry{},
		}
	}
	sr.LatestSeqNum = dr.SubmittedProof.SeqNum
	sr.LastUpdated = now

	if err := putState(ctx, "SR:"+satPairID, sr); err != nil {
		return err
	}
	if err := ctx.GetStub().DelState("DR:" + satPairID); err != nil {
		return err
	}
	emit(ctx, EventSettlementFinalized, map[string]interface{}{
		"satPairId": satPairID,
		"seqNum":    sr.LatestSeqNum,
	})
	return nil
}

// ── Top-Up ────────────────────────────────────────────────────────────────────

// RequestTopUp creates a pending top-up request to replenish an operator channel.
func (c *ISLSettlementChaincode) RequestTopUp(
	ctx contractapi.TransactionContextInterface,
	operatorChannelID string,
	amountA, amountB float64,
) error {
	if _, err := getOperatorChannel(ctx, operatorChannelID); err != nil {
		return err
	}
	msp, _ := ctx.GetClientIdentity().GetMSPID()
	now := nowSec(ctx)
	req := TopUpRequest{
		ID:                fmt.Sprintf("TU-%d", now),
		OperatorChannelID: operatorChannelID,
		RequestedBy:       msp,
		AmountA:           amountA,
		AmountB:           amountB,
		Status:            "PENDING",
		RequestedAt:       now * 1000,
		ExpiresAt:         (now + TTopupConfirmSec) * 1000,
	}
	if err := putState(ctx, "TU:"+req.ID, req); err != nil {
		return err
	}
	emit(ctx, EventTopUpRequested, map[string]interface{}{
		"topUpId": req.ID, "operatorChannelId": operatorChannelID,
		"requestedBy": msp,
	})
	return nil
}

// ConfirmTopUp confirms a pending top-up from the counterpart operator.
func (c *ISLSettlementChaincode) ConfirmTopUp(
	ctx contractapi.TransactionContextInterface,
	topUpID string,
) error {
	raw, err := ctx.GetStub().GetState("TU:" + topUpID)
	if err != nil || raw == nil {
		return fmt.Errorf("top-up %q not found", topUpID)
	}
	var req TopUpRequest
	if err := json.Unmarshal(raw, &req); err != nil {
		return err
	}
	if req.Status != "PENDING" {
		return fmt.Errorf("top-up %q is not PENDING (status: %s)", topUpID, req.Status)
	}
	if nowSec(ctx)*1000 > req.ExpiresAt {
		req.Status = "EXPIRED"
		_ = putState(ctx, "TU:"+topUpID, req)
		emit(ctx, EventTopUpExpired, map[string]string{"topUpId": topUpID})
		return fmt.Errorf("top-up %q has expired", topUpID)
	}
	msp, _ := ctx.GetClientIdentity().GetMSPID()
	if msp == req.RequestedBy {
		return fmt.Errorf("caller %q cannot confirm their own top-up request", msp)
	}
	opCh, err := getOperatorChannel(ctx, req.OperatorChannelID)
	if err != nil {
		return err
	}
	opCh.BalanceA += req.AmountA
	opCh.BalanceB += req.AmountB
	if err := putState(ctx, "OCH:"+req.OperatorChannelID, opCh); err != nil {
		return err
	}
	req.Status = "CONFIRMED"
	req.ConfirmedBy = msp
	if err := putState(ctx, "TU:"+topUpID, req); err != nil {
		return err
	}
	emit(ctx, EventTopUpConfirmed, map[string]string{"topUpId": topUpID})
	return nil
}

// ── ISL Out-of-Service Logging ────────────────────────────────────────────────

// RecordISLPause records an out-of-service entry against the SettlementRecord
// for the satellite pair.  The record is created lazily if it does not yet exist.
func (c *ISLSettlementChaincode) RecordISLPause(
	ctx contractapi.TransactionContextInterface,
	satPairID, opChannelID, reason string,
) error {
	sr, err := ensureSettlementRecord(ctx, satPairID, opChannelID)
	if err != nil {
		return err
	}
	msp, _ := ctx.GetClientIdentity().GetMSPID()
	entry := OutOfServiceEntry{
		ChannelID:   satPairID,
		PausedAt:    nowMs(ctx),
		Reason:      reason,
		InitiatedBy: msp,
	}
	sr.OutOfServiceLog = append(sr.OutOfServiceLog, entry)
	sr.LastUpdated = nowMs(ctx)
	if err := putState(ctx, "SR:"+satPairID, sr); err != nil {
		return err
	}
	emit(ctx, EventISLPaused, map[string]string{
		"satPairId": satPairID, "reason": reason,
	})
	return nil
}

// RecordISLResume records per-satellite ACK; sets ResumedAt when both ACKed.
func (c *ISLSettlementChaincode) RecordISLResume(
	ctx contractapi.TransactionContextInterface,
	satPairID, satelliteID string,
) error {
	sr, err := getSettlementRecord(ctx, satPairID)
	if err != nil {
		return err
	}
	if sr == nil {
		return fmt.Errorf("no settlement record for pair %q", satPairID)
	}
	if len(sr.OutOfServiceLog) == 0 {
		return fmt.Errorf("no OOS entries for pair %q", satPairID)
	}
	// Find most recent open OOS entry.
	idx := -1
	for i := len(sr.OutOfServiceLog) - 1; i >= 0; i-- {
		if sr.OutOfServiceLog[i].ResumedAt == 0 {
			idx = i
			break
		}
	}
	if idx < 0 {
		return fmt.Errorf("no open OOS entry for pair %q", satPairID)
	}
	now := nowMs(ctx)
	entry := &sr.OutOfServiceLog[idx]
	switch satelliteID {
	case sr.SatA:
		entry.SatAAckAt = now
	case sr.SatB:
		entry.SatBAckAt = now
	default:
		return fmt.Errorf("satellite %q is not in pair %q", satelliteID, satPairID)
	}
	if entry.SatAAckAt != 0 && entry.SatBAckAt != 0 {
		maxAck := entry.SatAAckAt
		if entry.SatBAckAt > maxAck {
			maxAck = entry.SatBAckAt
		}
		entry.ResumedAt = maxAck
		emit(ctx, EventISLResumed, map[string]interface{}{
			"satPairId": satPairID, "resumedAt": maxAck,
		})
	}
	sr.LastUpdated = now
	return putState(ctx, "SR:"+satPairID, sr)
}

// ── Notifications ─────────────────────────────────────────────────────────────

// GetPendingNotifications returns unacknowledged notifications for an operator.
// Requires CouchDB (rich query); uses the state DB query interface.
func (c *ISLSettlementChaincode) GetPendingNotifications(
	ctx contractapi.TransactionContextInterface,
	operatorID string,
) ([]*SettlementNotification, error) {
	query := fmt.Sprintf(
		`{"selector":{"targetOperator":%s,"acknowledged":false}}`,
		jsonStr(operatorID),
	)
	iter, err := ctx.GetStub().GetQueryResult(query)
	if err != nil {
		return nil, err
	}
	defer iter.Close()
	var results []*SettlementNotification
	for iter.HasNext() {
		kv, err := iter.Next()
		if err != nil {
			return nil, err
		}
		var n SettlementNotification
		if err := json.Unmarshal(kv.Value, &n); err != nil {
			continue
		}
		results = append(results, &n)
	}
	return results, nil
}

// AcknowledgeNotification marks a notification as acknowledged.
func (c *ISLSettlementChaincode) AcknowledgeNotification(
	ctx contractapi.TransactionContextInterface,
	notifID string,
) error {
	raw, err := ctx.GetStub().GetState("NOTIF:" + notifID)
	if err != nil || raw == nil {
		return fmt.Errorf("notification %q not found", notifID)
	}
	var n SettlementNotification
	if err := json.Unmarshal(raw, &n); err != nil {
		return err
	}
	now := nowMs(ctx)
	n.Acknowledged = true
	n.AcknowledgedAt = now
	return putState(ctx, "NOTIF:"+notifID, n)
}

// GetSettlementRecord returns the SettlementRecord for a satellite pair, or nil
// if no settlement has been committed yet.  Used by peer GSs to detect whether
// a balance reset was already committed before issuing a duplicate call.
func (c *ISLSettlementChaincode) GetSettlementRecord(
	ctx contractapi.TransactionContextInterface,
	satPairID string,
) (*SettlementRecord, error) {
	return getSettlementRecord(ctx, satPairID)
}
