package main

import (
	"crypto/ecdsa"
	"crypto/x509"
	"encoding/json"
	"testing"
	"time"

	"github.com/golang/protobuf/ptypes/timestamp"
	"github.com/hyperledger/fabric-chaincode-go/pkg/cid"
	"github.com/hyperledger/fabric-chaincode-go/shim"
	"github.com/hyperledger/fabric-chaincode-go/shimtest"
	"github.com/stretchr/testify/require"
)

// ── Mock transaction context ──────────────────────────────────────────────────

// mockIdentity satisfies cid.ClientIdentity with a configurable MSP ID.
type mockIdentity struct{ mspID string }

func (m *mockIdentity) GetID() (string, error)                          { return m.mspID, nil }
func (m *mockIdentity) GetMSPID() (string, error)                       { return m.mspID, nil }
func (m *mockIdentity) GetAttributeValue(string) (string, bool, error) { return "", false, nil }
func (m *mockIdentity) AssertAttributeValue(string, string) error       { return nil }
func (m *mockIdentity) GetX509Certificate() (*x509.Certificate, error) { return nil, nil }

// Compile-time check.
var _ cid.ClientIdentity = (*mockIdentity)(nil)

type mockCtx struct {
	stub     *shimtest.MockStub
	identity *mockIdentity
}

func (m *mockCtx) GetStub() shim.ChaincodeStubInterface { return m.stub }
func (m *mockCtx) GetClientIdentity() cid.ClientIdentity { return m.identity }

// newCtx creates a fresh mock context with an active transaction.
func newCtx(mspID string) *mockCtx {
	stub := shimtest.NewMockStub("lios-test", nil)
	stub.MockTransactionStart("tx-1")
	return &mockCtx{stub: stub, identity: &mockIdentity{mspID: mspID}}
}

// setTime overrides the stub's transaction timestamp.
func setTime(ctx *mockCtx, unixSec int64) {
	ctx.stub.TxTimestamp = &timestamp.Timestamp{Seconds: unixSec}
}

// putJSON writes an object directly to the stub's state (for test setup).
func putJSON(ctx *mockCtx, key string, v interface{}) {
	b, _ := json.Marshal(v)
	ctx.stub.State[key] = b
}

// getJSON reads a key from state and unmarshals it into out.
func getJSON(ctx *mockCtx, key string, out interface{}) bool {
	b := ctx.stub.State[key]
	if b == nil {
		return false
	}
	return json.Unmarshal(b, out) == nil
}

// ── Test constants ────────────────────────────────────────────────────────────

const (
	opA     = "OperatorA"
	opB     = "OperatorB"
	opChID  = "OperatorA_OperatorB_ch"
	satA    = "alpha-1"
	satB    = "beta-1"
	satChID = "alpha-1__beta-1" // satPairID
	mspA    = "OperatorAMSP"
	mspB    = "OperatorBMSP"
)

var cc = &ISLSettlementChaincode{}

// setupOpChannel opens the standard operator channel.
func setupOpChannel(t *testing.T, ctx *mockCtx) {
	t.Helper()
	require.NoError(t, cc.EnsureOperatorChannel(ctx, opA, opB, 5000.0, 5000.0, 500.0, 500.0))
}

// registerKeys registers PEM public keys on-chain for satA and satB.
func registerKeys(t *testing.T, ctx *mockCtx, pemA, pemB string) {
	t.Helper()
	require.NoError(t, cc.UpsertSatelliteKey(ctx, opA, satA, pemA))
	require.NoError(t, cc.UpsertSatelliteKey(ctx, opB, satB, pemB))
}

// fullSetup opens the operator channel and registers satellite keys.
// Returns private keys for satA and satB.
func fullSetup(t *testing.T) (*mockCtx, *ecdsa.PrivateKey, *ecdsa.PrivateKey) {
	t.Helper()
	ctx := newCtx(mspA)
	privA, pemA := genKey(t)
	privB, pemB := genKey(t)
	setupOpChannel(t, ctx)
	registerKeys(t, ctx, pemA, pemB)
	return ctx, privA, privB
}

// pendingCtx creates an operator channel + keys then puts the pair in a pending
// dispute (seq=5, signed only by satA).
func pendingCtx(t *testing.T) (*mockCtx, *ecdsa.PrivateKey, *ecdsa.PrivateKey) {
	t.Helper()
	ctx, privA, privB := fullSetup(t)
	p := makeProof(satChID, 5, 800.0, 1200.0, privA, nil)
	require.NoError(t, cc.InitiateSettlement(ctx, satChID, opChID, proofJSON(t, p)))
	return ctx, privA, privB
}

// ── EnsureOperatorChannel ─────────────────────────────────────────────────────

func TestEnsureOperatorChannel_Creates(t *testing.T) {
	ctx := newCtx(mspA)
	require.NoError(t, cc.EnsureOperatorChannel(ctx, opA, opB, 5000.0, 5000.0, 500.0, 500.0))

	var ch OperatorChannel
	require.True(t, getJSON(ctx, "OCH:"+opChID, &ch))
	require.Equal(t, opChID, ch.ID)
	require.Equal(t, opA, ch.OperatorA)
	require.Equal(t, 5000.0, ch.BalanceA)
	require.Equal(t, 500.0, ch.PenaltyReserveA)
	require.Equal(t, "OPEN", ch.Status)
}

func TestEnsureOperatorChannel_Idempotent(t *testing.T) {
	ctx := newCtx(mspA)
	require.NoError(t, cc.EnsureOperatorChannel(ctx, opA, opB, 5000.0, 5000.0, 500.0, 500.0))
	// Second call with different values must be a no-op.
	require.NoError(t, cc.EnsureOperatorChannel(ctx, opA, opB, 9999.0, 9999.0, 0.0, 0.0))

	var ch OperatorChannel
	getJSON(ctx, "OCH:"+opChID, &ch)
	require.Equal(t, 5000.0, ch.BalanceA, "balance must not change on second call")
}

// ── Key management ────────────────────────────────────────────────────────────

func TestUpsertSatelliteKey_RegisterAndOverwrite(t *testing.T) {
	ctx := newCtx(mspA)
	_, pem1 := genKey(t)
	_, pem2 := genKey(t)

	require.NoError(t, cc.UpsertSatelliteKey(ctx, opA, satA, pem1))
	k, err := cc.GetSatelliteKey(ctx, satA)
	require.NoError(t, err)
	require.Equal(t, pem1, k.PubKeyPEM)
	require.False(t, k.IsRevoked)

	require.NoError(t, cc.UpsertSatelliteKey(ctx, opA, satA, pem2))
	k2, _ := cc.GetSatelliteKey(ctx, satA)
	require.Equal(t, pem2, k2.PubKeyPEM)
}

func TestGetSatelliteKey_NotFound(t *testing.T) {
	ctx := newCtx(mspA)
	_, err := cc.GetSatelliteKey(ctx, "nonexistent-sat")
	require.Error(t, err)
}

func TestRevokeSatelliteKey(t *testing.T) {
	ctx := newCtx(mspA)
	_, pemA := genKey(t)
	require.NoError(t, cc.UpsertSatelliteKey(ctx, opA, satA, pemA))
	require.NoError(t, cc.RevokeSatelliteKey(ctx, opA, satA))

	k, _ := cc.GetSatelliteKey(ctx, satA)
	require.True(t, k.IsRevoked)
	require.NotZero(t, k.RevokedAt)
}

func TestRevokeSatelliteKey_WrongMSP(t *testing.T) {
	ctx := newCtx(mspB) // mspB tries to revoke opA's satellite
	_, pemA := genKey(t)
	require.NoError(t, cc.UpsertSatelliteKey(ctx, opA, satA, pemA))

	err := cc.RevokeSatelliteKey(ctx, opA, satA)
	require.Error(t, err)
	require.Contains(t, err.Error(), "cannot revoke")
}

// ── SubmitCoSignedSettlement ──────────────────────────────────────────────────

func TestSubmitCoSignedSettlement_Success(t *testing.T) {
	ctx, privA, privB := fullSetup(t)
	proof := makeProof(satChID, 10, 600.0, 1400.0, privA, privB)
	require.NoError(t, cc.SubmitCoSignedSettlement(ctx, satChID, opChID, proofJSON(t, proof)))

	var sr SettlementRecord
	require.True(t, getJSON(ctx, "SR:"+satChID, &sr))
	require.Equal(t, int64(10), sr.LatestSeqNum)
	require.Equal(t, satA, sr.SatA)
	require.Equal(t, satB, sr.SatB)

	// Operator channel balance unchanged — no fund-locking in the new design.
	var opCh OperatorChannel
	getJSON(ctx, "OCH:"+opChID, &opCh)
	require.InDelta(t, 5000.0, opCh.BalanceA, 1e-9)
	require.InDelta(t, 5000.0, opCh.BalanceB, 1e-9)
}

func TestSubmitCoSignedSettlement_ReSettlement(t *testing.T) {
	ctx, privA, privB := fullSetup(t)

	p1 := makeProof(satChID, 5, 700.0, 1300.0, privA, privB)
	require.NoError(t, cc.SubmitCoSignedSettlement(ctx, satChID, opChID, proofJSON(t, p1)))

	// Higher seq — must be accepted.
	p2 := makeProof(satChID, 12, 400.0, 1600.0, privA, privB)
	require.NoError(t, cc.SubmitCoSignedSettlement(ctx, satChID, opChID, proofJSON(t, p2)))

	var sr SettlementRecord
	getJSON(ctx, "SR:"+satChID, &sr)
	require.Equal(t, int64(12), sr.LatestSeqNum)
}

func TestSubmitCoSignedSettlement_StaleSeqNum(t *testing.T) {
	ctx, privA, privB := fullSetup(t)

	p1 := makeProof(satChID, 8, 500.0, 1500.0, privA, privB)
	require.NoError(t, cc.SubmitCoSignedSettlement(ctx, satChID, opChID, proofJSON(t, p1)))

	// Same seq — idempotent (second GS submission in mutual-finalization path).
	p2 := makeProof(satChID, 8, 500.0, 1500.0, privA, privB)
	require.NoError(t, cc.SubmitCoSignedSettlement(ctx, satChID, opChID, proofJSON(t, p2)))

	// Lower seq — rollback attempt; must be rejected.
	p3 := makeProof(satChID, 5, 600.0, 1400.0, privA, privB)
	err := cc.SubmitCoSignedSettlement(ctx, satChID, opChID, proofJSON(t, p3))
	require.Error(t, err)
	require.Contains(t, err.Error(), "rollback attempt")
}

func TestSubmitCoSignedSettlement_MissingSig(t *testing.T) {
	ctx, privA, _ := fullSetup(t)
	// Only sigA — sigB absent.
	p := makeProof(satChID, 1, 500.0, 1500.0, privA, nil)
	err := cc.SubmitCoSignedSettlement(ctx, satChID, opChID, proofJSON(t, p))
	require.Error(t, err)
	require.Contains(t, err.Error(), "both sigA and sigB")
}

func TestSubmitCoSignedSettlement_BadSig(t *testing.T) {
	ctx := newCtx(mspA)
	privA, pemA := genKey(t)
	privB, _ := genKey(t)
	_, pemBwrong := genKey(t) // register a different key for satB
	setupOpChannel(t, ctx)
	registerKeys(t, ctx, pemA, pemBwrong)

	// privB signs with the real key, but on-chain pemBwrong is registered.
	p := makeProof(satChID, 1, 500.0, 1500.0, privA, privB)
	err := cc.SubmitCoSignedSettlement(ctx, satChID, opChID, proofJSON(t, p))
	require.Error(t, err)
	require.Contains(t, err.Error(), "verification failed")
}

func TestSubmitCoSignedSettlement_NoSatKey(t *testing.T) {
	ctx := newCtx(mspA)
	privA, _ := genKey(t)
	privB, _ := genKey(t)
	setupOpChannel(t, ctx)
	// Keys NOT registered — should fail with "no key for ..."
	p := makeProof(satChID, 1, 500.0, 1500.0, privA, privB)
	err := cc.SubmitCoSignedSettlement(ctx, satChID, opChID, proofJSON(t, p))
	require.Error(t, err)
	require.Contains(t, err.Error(), "no key for")
}

// ── SubmitBalanceReset ────────────────────────────────────────────────────────

func TestSubmitBalanceReset_Success(t *testing.T) {
	ctx, privA, privB := fullSetup(t)
	// satA forwarded 800 KB → balA=200, balB=1800
	proof := makeProof(satChID, 10, 200.0, 1800.0, privA, privB)
	require.NoError(t, cc.SubmitBalanceReset(ctx, satChID, opChID, proofJSON(t, proof)))

	var sr SettlementRecord
	require.True(t, getJSON(ctx, "SR:"+satChID, &sr))
	require.Equal(t, int64(10), sr.LatestSeqNum)

	// delta = (200+1800)/2 - 200 = 800; B pays A
	// opCh.BalanceA += 800, opCh.BalanceB -= 800
	var opCh OperatorChannel
	getJSON(ctx, "OCH:"+opChID, &opCh)
	require.InDelta(t, 5800.0, opCh.BalanceA, 1e-9, "A receives payment")
	require.InDelta(t, 4200.0, opCh.BalanceB, 1e-9, "B pays for service received")
}

func TestSubmitBalanceReset_BForwarded(t *testing.T) {
	ctx, privA, privB := fullSetup(t)
	// satB forwarded 300 KB → balA=1300, balB=700
	proof := makeProof(satChID, 5, 1300.0, 700.0, privA, privB)
	require.NoError(t, cc.SubmitBalanceReset(ctx, satChID, opChID, proofJSON(t, proof)))

	// delta = (1300+700)/2 - 1300 = -300; A pays B
	var opCh OperatorChannel
	getJSON(ctx, "OCH:"+opChID, &opCh)
	require.InDelta(t, 4700.0, opCh.BalanceA, 1e-9, "A pays for service received")
	require.InDelta(t, 5300.0, opCh.BalanceB, 1e-9, "B receives payment")
}

func TestSubmitBalanceReset_RequiresBothSigs(t *testing.T) {
	ctx, privA, _ := fullSetup(t)
	proof := makeProof(satChID, 3, 200.0, 1800.0, privA, nil)
	err := cc.SubmitBalanceReset(ctx, satChID, opChID, proofJSON(t, proof))
	require.Error(t, err)
	require.Contains(t, err.Error(), "co-signed proof")
}

func TestSubmitBalanceReset_InsufficientBalance(t *testing.T) {
	ctx := newCtx(mspA)
	// Operator channel with tiny balance so B can't cover the delta
	require.NoError(t, cc.EnsureOperatorChannel(ctx, opA, opB, 500.0, 100.0, 10.0, 10.0))
	privA, pemA := genKey(t)
	privB, pemB := genKey(t)
	registerKeys(t, ctx, pemA, pemB)
	// satA forwarded 800 KB — delta=800, B needs 800 but only has 100
	proof := makeProof(satChID, 1, 200.0, 1800.0, privA, privB)
	err := cc.SubmitBalanceReset(ctx, satChID, opChID, proofJSON(t, proof))
	require.Error(t, err)
	require.Contains(t, err.Error(), "insufficient balance")
}

// ── InitiateSettlement ────────────────────────────────────────────────────────

func TestInitiateSettlement_Success(t *testing.T) {
	ctx, privA, _ := fullSetup(t)
	p := makeProof(satChID, 3, 800.0, 1200.0, privA, nil)
	require.NoError(t, cc.InitiateSettlement(ctx, satChID, opChID, proofJSON(t, p)))

	var dr DisputeRecord
	require.True(t, getJSON(ctx, "DR:"+satChID, &dr))
	require.NotNil(t, dr.SubmittedProof)
	require.Equal(t, int64(3), dr.SubmittedProof.SeqNum)
}

func TestInitiateSettlement_AlreadyPending(t *testing.T) {
	ctx, privA, _ := pendingCtx(t)
	p2 := makeProof(satChID, 6, 700.0, 1300.0, privA, nil)
	err := cc.InitiateSettlement(ctx, satChID, opChID, proofJSON(t, p2))
	require.Error(t, err)
	require.Contains(t, err.Error(), "already pending")
}

func TestInitiateSettlement_StaleSeq(t *testing.T) {
	ctx, privA, _ := fullSetup(t)
	// Inject a settlement record already at seqNum=5.
	sr := SettlementRecord{
		SatPairID: satChID, SatA: satA, SatB: satB,
		OperatorChannelID: opChID, LatestSeqNum: 5,
		OutOfServiceLog: []OutOfServiceEntry{},
	}
	putJSON(ctx, "SR:"+satChID, sr)

	p := makeProof(satChID, 5, 500.0, 1500.0, privA, nil) // seqNum == current → rejected
	err := cc.InitiateSettlement(ctx, satChID, opChID, proofJSON(t, p))
	require.Error(t, err)
	require.Contains(t, err.Error(), "seqNum")
}

// ── ChallengeSettlement ───────────────────────────────────────────────────────

func TestChallengeSettlement_CoSignedFinalizes(t *testing.T) {
	ctx, privA, privB := pendingCtx(t)
	counter := makeProof(satChID, 8, 400.0, 1600.0, privA, privB)
	require.NoError(t, cc.ChallengeSettlement(ctx, satChID, proofJSON(t, counter)))

	var sr SettlementRecord
	require.True(t, getJSON(ctx, "SR:"+satChID, &sr))
	require.Equal(t, int64(8), sr.LatestSeqNum, "co-signed counter must finalize immediately")

	// DisputeRecord must be deleted.
	require.Nil(t, ctx.stub.State["DR:"+satChID])
}

func TestChallengeSettlement_SingleSigStaysPending(t *testing.T) {
	ctx, _, privB := pendingCtx(t)
	counter := makeProof(satChID, 8, 400.0, 1600.0, nil, privB)
	require.NoError(t, cc.ChallengeSettlement(ctx, satChID, proofJSON(t, counter)))

	// DisputeRecord still exists, updated to the newer proof.
	var dr DisputeRecord
	require.True(t, getJSON(ctx, "DR:"+satChID, &dr))
	require.Equal(t, int64(8), dr.SubmittedProof.SeqNum)
}

func TestChallengeSettlement_SlashesPenalty(t *testing.T) {
	ctx, _, privB := pendingCtx(t)

	counter := makeProof(satChID, 8, 400.0, 1600.0, nil, privB)
	require.NoError(t, cc.ChallengeSettlement(ctx, satChID, proofJSON(t, counter)))

	// InitiateSettlement was submitted by mspA → reserve A should be zeroed.
	var opCh OperatorChannel
	getJSON(ctx, "OCH:"+opChID, &opCh)
	require.Equal(t, 0.0, opCh.PenaltyReserveA)
	require.Equal(t, 1000.0, opCh.PenaltyReserveB) // A's 500 transferred to B
}

func TestChallengeSettlement_WindowExpired(t *testing.T) {
	ctx, privA, privB := fullSetup(t)

	// Submit unilateral settlement at an old time.
	oldTime := time.Now().Unix() - TChallengeWindowSec - 10
	setTime(ctx, oldTime)
	p := makeProof(satChID, 5, 800.0, 1200.0, privA, nil)
	require.NoError(t, cc.InitiateSettlement(ctx, satChID, opChID, proofJSON(t, p)))

	// Challenge at current time (window expired).
	setTime(ctx, time.Now().Unix())
	counter := makeProof(satChID, 8, 400.0, 1600.0, privA, privB)
	err := cc.ChallengeSettlement(ctx, satChID, proofJSON(t, counter))
	require.Error(t, err)
	require.Contains(t, err.Error(), "expired")
}

func TestChallengeSettlement_StaleCounter(t *testing.T) {
	ctx, _, privB := pendingCtx(t)
	counter := makeProof(satChID, 4, 600.0, 1400.0, nil, privB) // seq 4 < submitted 5
	err := cc.ChallengeSettlement(ctx, satChID, proofJSON(t, counter))
	require.Error(t, err)
	require.Contains(t, err.Error(), "not newer")
}

func TestChallengeSettlement_NoPendingDispute(t *testing.T) {
	ctx, privA, privB := fullSetup(t)
	counter := makeProof(satChID, 3, 500.0, 1500.0, privA, privB)
	err := cc.ChallengeSettlement(ctx, satChID, proofJSON(t, counter))
	require.Error(t, err)
	require.Contains(t, err.Error(), "no pending dispute")
}

// ── FinalizeSettlement ────────────────────────────────────────────────────────

func TestFinalizeSettlement_AfterWindow(t *testing.T) {
	ctx, privA, _ := fullSetup(t)

	oldTime := time.Now().Unix() - TChallengeWindowSec - 5
	setTime(ctx, oldTime)
	p := makeProof(satChID, 7, 300.0, 1700.0, privA, nil)
	require.NoError(t, cc.InitiateSettlement(ctx, satChID, opChID, proofJSON(t, p)))

	setTime(ctx, time.Now().Unix())
	require.NoError(t, cc.FinalizeSettlement(ctx, satChID))

	var sr SettlementRecord
	require.True(t, getJSON(ctx, "SR:"+satChID, &sr))
	require.Equal(t, int64(7), sr.LatestSeqNum)

	// DisputeRecord deleted.
	require.Nil(t, ctx.stub.State["DR:"+satChID])
}

func TestFinalizeSettlement_BeforeWindow(t *testing.T) {
	ctx, privA, _ := fullSetup(t)
	p := makeProof(satChID, 7, 300.0, 1700.0, privA, nil)
	require.NoError(t, cc.InitiateSettlement(ctx, satChID, opChID, proofJSON(t, p)))

	err := cc.FinalizeSettlement(ctx, satChID) // window still open
	require.Error(t, err)
	require.Contains(t, err.Error(), "not expired")
}

func TestFinalizeSettlement_NoPendingDispute(t *testing.T) {
	ctx, _, _ := fullSetup(t)
	err := cc.FinalizeSettlement(ctx, satChID)
	require.Error(t, err)
	require.Contains(t, err.Error(), "no pending dispute")
}

// ── Top-Up ────────────────────────────────────────────────────────────────────

func TestRequestTopUp_Success(t *testing.T) {
	ctx := newCtx(mspA)
	setupOpChannel(t, ctx)
	require.NoError(t, cc.RequestTopUp(ctx, opChID, 200.0, 200.0))

	found := false
	for k := range ctx.stub.State {
		if len(k) > 3 && k[:3] == "TU:" {
			found = true
			var req TopUpRequest
			getJSON(ctx, k, &req)
			require.Equal(t, "PENDING", req.Status)
			require.Equal(t, 200.0, req.AmountA)
		}
	}
	require.True(t, found)
}

func TestRequestTopUp_NoChannel(t *testing.T) {
	ctx := newCtx(mspA)
	err := cc.RequestTopUp(ctx, opChID, 200.0, 200.0)
	require.Error(t, err)
}

// findTopUpID returns the top-up request ID stored in stub state.
func findTopUpID(ctx *mockCtx) string {
	for k := range ctx.stub.State {
		if len(k) > 3 && k[:3] == "TU:" {
			return k[3:]
		}
	}
	return ""
}

func TestConfirmTopUp_Success(t *testing.T) {
	ctx := newCtx(mspA)
	setupOpChannel(t, ctx)
	require.NoError(t, cc.RequestTopUp(ctx, opChID, 300.0, 300.0))
	topUpID := findTopUpID(ctx)
	require.NotEmpty(t, topUpID)

	var before OperatorChannel
	getJSON(ctx, "OCH:"+opChID, &before)

	// Confirm from a different operator (mspB).
	ctx.identity.mspID = mspB
	require.NoError(t, cc.ConfirmTopUp(ctx, topUpID))

	var after OperatorChannel
	getJSON(ctx, "OCH:"+opChID, &after)
	require.InDelta(t, before.BalanceA+300.0, after.BalanceA, 1e-9)

	var req TopUpRequest
	getJSON(ctx, "TU:"+topUpID, &req)
	require.Equal(t, "CONFIRMED", req.Status)
	require.Equal(t, mspB, req.ConfirmedBy)
}

func TestConfirmTopUp_OwnRequest(t *testing.T) {
	ctx := newCtx(mspA)
	setupOpChannel(t, ctx)
	require.NoError(t, cc.RequestTopUp(ctx, opChID, 300.0, 300.0))
	topUpID := findTopUpID(ctx)
	// Same MSP tries to confirm its own request.
	err := cc.ConfirmTopUp(ctx, topUpID)
	require.Error(t, err)
	require.Contains(t, err.Error(), "cannot confirm their own")
}

func TestConfirmTopUp_Expired(t *testing.T) {
	ctx := newCtx(mspA)
	setupOpChannel(t, ctx)

	oldTime := time.Now().Unix() - TTopupConfirmSec - 10
	setTime(ctx, oldTime)
	require.NoError(t, cc.RequestTopUp(ctx, opChID, 300.0, 300.0))
	topUpID := findTopUpID(ctx)

	setTime(ctx, time.Now().Unix())
	ctx.identity.mspID = mspB
	err := cc.ConfirmTopUp(ctx, topUpID)
	require.Error(t, err)
	require.Contains(t, err.Error(), "expired")
}

// ── ISL Pause / Resume ────────────────────────────────────────────────────────

func TestRecordISLPause(t *testing.T) {
	ctx := newCtx(mspA)
	setupOpChannel(t, ctx)

	require.NoError(t, cc.RecordISLPause(ctx, satChID, opChID, "SETTLEMENT"))

	var sr SettlementRecord
	require.True(t, getJSON(ctx, "SR:"+satChID, &sr))
	require.Len(t, sr.OutOfServiceLog, 1)
	require.Equal(t, "SETTLEMENT", sr.OutOfServiceLog[0].Reason)
	require.Zero(t, sr.OutOfServiceLog[0].ResumedAt)
}

func TestRecordISLResume_PartialThenFull(t *testing.T) {
	ctx := newCtx(mspA)
	setupOpChannel(t, ctx)
	require.NoError(t, cc.RecordISLPause(ctx, satChID, opChID, "SETTLEMENT"))

	// satA ACKs first — still paused.
	require.NoError(t, cc.RecordISLResume(ctx, satChID, satA))
	var sr1 SettlementRecord
	getJSON(ctx, "SR:"+satChID, &sr1)
	require.NotZero(t, sr1.OutOfServiceLog[0].SatAAckAt)
	require.Zero(t, sr1.OutOfServiceLog[0].SatBAckAt)

	// satB ACKs — ResumedAt is set.
	require.NoError(t, cc.RecordISLResume(ctx, satChID, satB))
	var sr2 SettlementRecord
	getJSON(ctx, "SR:"+satChID, &sr2)
	require.NotZero(t, sr2.OutOfServiceLog[0].ResumedAt)
}

func TestRecordISLResume_UnknownSatellite(t *testing.T) {
	ctx := newCtx(mspA)
	setupOpChannel(t, ctx)
	require.NoError(t, cc.RecordISLPause(ctx, satChID, opChID, "SETTLEMENT"))

	err := cc.RecordISLResume(ctx, satChID, "gamma-99")
	require.Error(t, err)
	require.Contains(t, err.Error(), "not in pair")
}

func TestRecordISLResume_NoRecord(t *testing.T) {
	ctx := newCtx(mspA)
	err := cc.RecordISLResume(ctx, satChID, satA)
	require.Error(t, err)
	require.Contains(t, err.Error(), "no settlement record")
}

func TestRecordISLResume_NoOOSEntry(t *testing.T) {
	ctx := newCtx(mspA)
	setupOpChannel(t, ctx)
	// Create the record without any OOS entries.
	sr := SettlementRecord{
		SatPairID: satChID, SatA: satA, SatB: satB,
		OperatorChannelID: opChID,
		OutOfServiceLog:   []OutOfServiceEntry{},
	}
	putJSON(ctx, "SR:"+satChID, sr)
	err := cc.RecordISLResume(ctx, satChID, satA)
	require.Error(t, err)
	require.Contains(t, err.Error(), "no OOS")
}

// ── AcknowledgeNotification ───────────────────────────────────────────────────

func TestAcknowledgeNotification(t *testing.T) {
	ctx := newCtx(mspA)
	notif := SettlementNotification{
		ID: "notif-1", TargetOperator: "OperatorA",
		Type: "SETTLEMENT_FINALIZED", Payload: `{}`, Acknowledged: false,
	}
	putJSON(ctx, "NOTIF:notif-1", notif)

	require.NoError(t, cc.AcknowledgeNotification(ctx, "notif-1"))

	var updated SettlementNotification
	getJSON(ctx, "NOTIF:notif-1", &updated)
	require.True(t, updated.Acknowledged)
	require.NotZero(t, updated.AcknowledgedAt)
}

func TestAcknowledgeNotification_NotFound(t *testing.T) {
	ctx := newCtx(mspA)
	err := cc.AcknowledgeNotification(ctx, "nonexistent")
	require.Error(t, err)
	require.Contains(t, err.Error(), "not found")
}
