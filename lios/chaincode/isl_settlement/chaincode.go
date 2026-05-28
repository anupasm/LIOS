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

// ── helpers ──────────────────────────────────────────────────────────────────

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

func getSatChannel(ctx contractapi.TransactionContextInterface, id string) (*SatChannel, error) {
	raw, err := ctx.GetStub().GetState("SCH:" + id)
	if err != nil || raw == nil {
		return nil, fmt.Errorf("sat channel %q not found", id)
	}
	var ch SatChannel
	if err := json.Unmarshal(raw, &ch); err != nil {
		return nil, err
	}
	return &ch, nil
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

// ── Channel Lifecycle ─────────────────────────────────────────────────────────

// OpenOperatorChannel opens a bilateral operator channel with penalty reserves.
// Both operator peers must endorse this transaction (enforced by channel policy).
func (c *ISLSettlementChaincode) OpenOperatorChannel(
	ctx contractapi.TransactionContextInterface,
	operatorA, operatorB string,
	balanceA, balanceB, reserveA, reserveB float64,
) error {
	chID := operatorA + "_" + operatorB + "_ch"
	existing, _ := ctx.GetStub().GetState("OCH:" + chID)
	if existing != nil {
		return fmt.Errorf("operator channel %q already exists", chID)
	}
	msp, err := ctx.GetClientIdentity().GetMSPID()
	if err != nil {
		return err
	}
	if msp != operatorA+"MSP" && msp != operatorB+"MSP" {
		return fmt.Errorf("caller MSP %q is not operatorA or operatorB", msp)
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

// RegisterSatChannel registers a satellite-pair sub-channel under an operator channel.
func (c *ISLSettlementChaincode) RegisterSatChannel(
	ctx contractapi.TransactionContextInterface,
	operatorChannelID, satA, satB string,
	balA, balB float64,
) error {
	opCh, err := getOperatorChannel(ctx, operatorChannelID)
	if err != nil {
		return err
	}
	if opCh.Status != "OPEN" {
		return fmt.Errorf("operator channel %q is not OPEN", operatorChannelID)
	}
	if opCh.BalanceA < balA || opCh.BalanceB < balB {
		return fmt.Errorf("insufficient operator channel balance (need A=%.2f B=%.2f)", balA, balB)
	}
	opCh.BalanceA -= balA
	opCh.BalanceB -= balB
	if err := putState(ctx, "OCH:"+operatorChannelID, opCh); err != nil {
		return err
	}

	satChID := satA + "__" + satB
	sch := SatChannel{
		ID:                satChID,
		OperatorChannelID: operatorChannelID,
		SatA:              satA,
		SatB:              satB,
		BalanceA:          balA,
		BalanceB:          balB,
		Status:            "ACTIVE",
		LastUpdated:       nowMs(ctx),
		OutOfServiceLog:   []OutOfServiceEntry{},
	}
	return putState(ctx, "SCH:"+satChID, sch)
}

// InitiateSettlement submits a BalanceProof and starts the T_challenge window.
func (c *ISLSettlementChaincode) InitiateSettlement(
	ctx contractapi.TransactionContextInterface,
	satChannelID string,
	proofJSON string,
) error {
	sch, err := getSatChannel(ctx, satChannelID)
	if err != nil {
		return err
	}
	var proof BalanceProof
	if err := json.Unmarshal([]byte(proofJSON), &proof); err != nil {
		return fmt.Errorf("invalid proof JSON: %w", err)
	}
	if proof.SeqNum <= sch.SeqNum {
		return fmt.Errorf("proof seqNum %d ≤ current %d", proof.SeqNum, sch.SeqNum)
	}

	// Verify ECDSA signatures
	keyA, err := getSatKey(ctx, sch.SatA)
	if err != nil {
		return fmt.Errorf("no key for %s: %w", sch.SatA, err)
	}
	keyB, err := getSatKey(ctx, sch.SatB)
	if err != nil {
		return fmt.Errorf("no key for %s: %w", sch.SatB, err)
	}
	if err := VerifyBalanceProof(proof, keyA.PubKeyPEM, keyB.PubKeyPEM); err != nil {
		return fmt.Errorf("signature verification failed: %w", err)
	}

	msp, _ := ctx.GetClientIdentity().GetMSPID()
	now := nowSec(ctx)
	proof.SubmittedBy = msp
	proof.SubmittedAt = now * 1000

	submitted := nowMs(ctx)
	sch.Status = "PENDING_SETTLEMENT"
	sch.SubmittedProof = &proof
	sch.SettlementSubmittedAt = &submitted

	if err := putState(ctx, "SCH:"+satChannelID, sch); err != nil {
		return err
	}
	emit(ctx, EventSettlementInitiated, map[string]interface{}{
		"satChannelId": satChannelID,
		"seqNum":       proof.SeqNum,
		"submittedBy":  msp,
	})
	return nil
}

// ChallengeSettlement allows the counterpart to submit a newer proof within T_challenge.
func (c *ISLSettlementChaincode) ChallengeSettlement(
	ctx contractapi.TransactionContextInterface,
	satChannelID string,
	counterProofJSON string,
) error {
	sch, err := getSatChannel(ctx, satChannelID)
	if err != nil {
		return err
	}
	if sch.Status != "PENDING_SETTLEMENT" {
		return fmt.Errorf("channel %q is not in PENDING_SETTLEMENT state", satChannelID)
	}
	if sch.SettlementSubmittedAt == nil {
		return fmt.Errorf("no settlement timestamp recorded")
	}
	deadline := *sch.SettlementSubmittedAt/1000 + TChallengeWindowSec
	if nowSec(ctx) > deadline {
		return fmt.Errorf("challenge window expired at %d", deadline)
	}

	var counter BalanceProof
	if err := json.Unmarshal([]byte(counterProofJSON), &counter); err != nil {
		return err
	}
	if sch.SubmittedProof == nil || counter.SeqNum <= sch.SubmittedProof.SeqNum {
		return fmt.Errorf("counter proof seqNum %d is not newer than submitted %d",
			counter.SeqNum, sch.SubmittedProof.SeqNum)
	}

	// Verify counter proof
	keyA, _ := getSatKey(ctx, sch.SatA)
	keyB, _ := getSatKey(ctx, sch.SatB)
	if keyA != nil && keyB != nil {
		if err := VerifyBalanceProof(counter, keyA.PubKeyPEM, keyB.PubKeyPEM); err != nil {
			return fmt.Errorf("counter proof verification failed: %w", err)
		}
	}

	// Adopt counter proof (revoke submitted; slash initiating party's reserve)
	opCh, err := getOperatorChannel(ctx, sch.OperatorChannelID)
	if err == nil {
		// Slash penalty: transfer reserve from initiating operator to challenger
		if sch.SubmittedProof.SubmittedBy == opCh.OperatorA+"MSP" {
			slashed := opCh.PenaltyReserveA
			opCh.PenaltyReserveA = 0
			opCh.PenaltyReserveB += slashed
		} else {
			slashed := opCh.PenaltyReserveB
			opCh.PenaltyReserveB = 0
			opCh.PenaltyReserveA += slashed
		}
		_ = putState(ctx, "OCH:"+sch.OperatorChannelID, opCh)
	}

	sch.SubmittedProof = &counter
	if err := putState(ctx, "SCH:"+satChannelID, sch); err != nil {
		return err
	}
	emit(ctx, EventSettlementChallenged, map[string]interface{}{
		"satChannelId": satChannelID,
		"counterSeq":   counter.SeqNum,
	})
	emit(ctx, EventPenaltyApplied, map[string]interface{}{"satChannelId": satChannelID})
	return nil
}

// FinalizeSettlement applies the accepted proof after T_challenge expires.
func (c *ISLSettlementChaincode) FinalizeSettlement(
	ctx contractapi.TransactionContextInterface,
	satChannelID string,
) error {
	sch, err := getSatChannel(ctx, satChannelID)
	if err != nil {
		return err
	}
	if sch.Status != "PENDING_SETTLEMENT" {
		return fmt.Errorf("channel %q is not in PENDING_SETTLEMENT state", satChannelID)
	}
	if sch.SettlementSubmittedAt == nil {
		return fmt.Errorf("no settlement timestamp")
	}
	deadline := *sch.SettlementSubmittedAt/1000 + TChallengeWindowSec
	if nowSec(ctx) < deadline {
		return fmt.Errorf("T_challenge has not expired yet (deadline: %d)", deadline)
	}
	if sch.SubmittedProof == nil {
		return fmt.Errorf("no submitted proof")
	}

	// Update operator channel balances
	opCh, err := getOperatorChannel(ctx, sch.OperatorChannelID)
	if err == nil {
		opCh.BalanceA += sch.SubmittedProof.BalanceA
		opCh.BalanceB += sch.SubmittedProof.BalanceB
		_ = putState(ctx, "OCH:"+sch.OperatorChannelID, opCh)
	}

	sch.Status = "SETTLED"
	sch.SeqNum = sch.SubmittedProof.SeqNum
	sch.BalanceA = sch.SubmittedProof.BalanceA
	sch.BalanceB = sch.SubmittedProof.BalanceB
	now := nowMs(ctx)
	sch.LastUpdated = now

	if err := putState(ctx, "SCH:"+satChannelID, sch); err != nil {
		return err
	}
	emit(ctx, EventSettlementFinalized, map[string]interface{}{
		"satChannelId": satChannelID,
		"seqNum":       sch.SeqNum,
	})
	return nil
}

// ── Top-Up ────────────────────────────────────────────────────────────────────

// RequestTopUp creates a pending top-up request.
func (c *ISLSettlementChaincode) RequestTopUp(
	ctx contractapi.TransactionContextInterface,
	operatorChannelID string,
	amountA, amountB float64,
) error {
	_, err := getOperatorChannel(ctx, operatorChannelID)
	if err != nil {
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
	})
	return nil
}

// ConfirmTopUp confirms a pending top-up from the counterpart.
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
	opCh, err := getOperatorChannel(ctx, req.OperatorChannelID)
	if err != nil {
		return err
	}
	msp, _ := ctx.GetClientIdentity().GetMSPID()
	if msp == req.RequestedBy {
		return fmt.Errorf("caller %q cannot confirm their own top-up request", msp)
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

// ── ISL Pause / Resume ─────────────────────────────────────────────────────────

// RecordISLPause records a channel pause with an out-of-service entry.
func (c *ISLSettlementChaincode) RecordISLPause(
	ctx contractapi.TransactionContextInterface,
	satChannelID, reason string,
) error {
	sch, err := getSatChannel(ctx, satChannelID)
	if err != nil {
		return err
	}
	msp, _ := ctx.GetClientIdentity().GetMSPID()
	entry := OutOfServiceEntry{
		ChannelID:   satChannelID,
		PausedAt:    nowMs(ctx),
		Reason:      reason,
		InitiatedBy: msp,
	}
	sch.OutOfServiceLog = append(sch.OutOfServiceLog, entry)
	sch.Status = "PAUSED_PENDING_SETTLEMENT"
	if err := putState(ctx, "SCH:"+satChannelID, sch); err != nil {
		return err
	}
	emit(ctx, EventISLPaused, map[string]string{
		"satChannelId": satChannelID, "reason": reason,
	})
	return nil
}

// RecordISLResume records per-satellite ACK; sets ResumedAt when both ACK.
func (c *ISLSettlementChaincode) RecordISLResume(
	ctx contractapi.TransactionContextInterface,
	satChannelID, satelliteID string,
) error {
	sch, err := getSatChannel(ctx, satChannelID)
	if err != nil {
		return err
	}
	if len(sch.OutOfServiceLog) == 0 {
		return fmt.Errorf("no OOS entries for %q", satChannelID)
	}
	// Find latest open OOS entry
	idx := -1
	for i := len(sch.OutOfServiceLog) - 1; i >= 0; i-- {
		if sch.OutOfServiceLog[i].ResumedAt == nil {
			idx = i
			break
		}
	}
	if idx < 0 {
		return fmt.Errorf("no open OOS entry for %q", satChannelID)
	}
	now := nowMs(ctx)
	entry := &sch.OutOfServiceLog[idx]
	if satelliteID == sch.SatA {
		entry.SatAAckAt = &now
	} else if satelliteID == sch.SatB {
		entry.SatBAckAt = &now
	} else {
		return fmt.Errorf("satellite %q is not in channel %q", satelliteID, satChannelID)
	}
	if entry.SatAAckAt != nil && entry.SatBAckAt != nil {
		maxAck := *entry.SatAAckAt
		if *entry.SatBAckAt > maxAck {
			maxAck = *entry.SatBAckAt
		}
		entry.ResumedAt = &maxAck
		sch.Status = "ACTIVE"
		emit(ctx, EventISLResumed, map[string]interface{}{
			"satChannelId": satChannelID,
			"resumedAt":    maxAck,
		})
	}
	return putState(ctx, "SCH:"+satChannelID, sch)
}

// ── Key Management ────────────────────────────────────────────────────────────

// RegisterSatelliteKey registers a satellite public key on-chain (after operator cert sig verify).
func (c *ISLSettlementChaincode) RegisterSatelliteKey(
	ctx contractapi.TransactionContextInterface,
	operatorID, satelliteID, pubKeyPEM, operatorCertSigHex string,
) error {
	// Verify operator signature over (satelliteID || pubKeyPEM)
	payload := satelliteID + pubKeyPEM
	opKeyRaw, err := ctx.GetStub().GetState("OPKEY:" + operatorID)
	if err != nil || opKeyRaw == nil {
		// If no operator root key registered yet, accept (bootstrap mode)
		// In production: return fmt.Errorf("operator %q root key not registered", operatorID)
		_ = operatorCertSigHex
	} else {
		if !VerifyECDSA(string(opKeyRaw), payload, operatorCertSigHex) {
			return fmt.Errorf("invalid operator cert signature for satellite %q", satelliteID)
		}
	}
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
	key.RevokedAt = &now
	if err := putState(ctx, "SAT:"+satelliteID, key); err != nil {
		return err
	}
	emit(ctx, EventKeyRevoked, map[string]string{
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

// ── Notifications ─────────────────────────────────────────────────────────────

// GetPendingNotifications returns unacknowledged notifications for an operator.
// Uses CouchDB rich query.
func (c *ISLSettlementChaincode) GetPendingNotifications(
	ctx contractapi.TransactionContextInterface,
	operatorID string,
) ([]*SettlementNotification, error) {
	query := fmt.Sprintf(`{"selector":{"targetOperator":"%s","acknowledged":false}}`, operatorID)
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

// AcknowledgeNotification marks a notification as acknowledged with timestamp.
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
	n.AcknowledgedAt = &now
	return putState(ctx, "NOTIF:"+notifID, n)
}
