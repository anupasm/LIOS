package main

// ── Operator-Level Channel ────────────────────────────────────────────────────

type OperatorChannel struct {
	ID              string  `json:"channelId"`
	OperatorA       string  `json:"operatorA"`
	OperatorB       string  `json:"operatorB"`
	BalanceA        float64 `json:"balanceA"`
	BalanceB        float64 `json:"balanceB"`
	PenaltyReserveA float64 `json:"penaltyReserveA"`
	PenaltyReserveB float64 `json:"penaltyReserveB"`
	Status          string  `json:"status"` // OPEN|SETTLING|DISPUTED|CLOSED
	OpenedAt        int64   `json:"openedAt"`
	ClosedAt        int64   `json:"closedAt,omitempty"`
}

// ── Settlement Record ─────────────────────────────────────────────────────────
//
// Replaces the heavyweight SatChannel.  Created lazily on the first settlement
// or ISL pause for a satellite pair — no upfront registration or fund-locking
// required.  Serves as the replay-prevention cursor (LatestSeqNum) and the
// on-chain anchor for the OOS log.  Satellite-pair public keys are looked up
// from the SatKey registry; operator-channel liquidity is managed independently.

type SettlementRecord struct {
	SatPairID         string              `json:"satPairId"`
	SatA              string              `json:"satelliteA"`
	SatB              string              `json:"satelliteB"`
	OperatorChannelID string              `json:"operatorChannelId"`
	LatestSeqNum      int64               `json:"latestSeqNum"`
	LastUpdated       int64               `json:"lastUpdated"`
	OutOfServiceLog   []OutOfServiceEntry `json:"outOfServiceLog"`
}

// ── Dispute Record ────────────────────────────────────────────────────────────
//
// Ephemeral object created by InitiateSettlement; deleted when the dispute is
// resolved (ChallengeSettlement with a co-signed counter-proof, or
// FinalizeSettlement after T_challenge expires).

type DisputeRecord struct {
	SatPairID         string        `json:"satPairId"`
	SatA              string        `json:"satelliteA"`
	SatB              string        `json:"satelliteB"`
	OperatorChannelID string        `json:"operatorChannelId"`
	SubmittedProof    *BalanceProof `json:"submittedProof"`
	SubmittedAt       int64         `json:"submittedAt"`
}

// ── Balance Proof ─────────────────────────────────────────────────────────────
// JSON tags match the keys used by blockchain_client._proof_to_chaincode_json().

type BalanceProof struct {
	ChannelID   string  `json:"satChannelId"`
	SeqNum      int64   `json:"seqNum"`
	BalanceA    float64 `json:"balanceA"`
	BalanceB    float64 `json:"balanceB"`
	SigA        string  `json:"sigA"`        // DER hex ECDSA P-256
	SigB        string  `json:"sigB"`
	SubmittedBy string  `json:"submittedBy"` // operator MSP ID
	SubmittedAt int64   `json:"submittedAt"`
}

// ── Out-of-Service Log Entry ──────────────────────────────────────────────────

type OutOfServiceEntry struct {
	ChannelID   string `json:"satPairId"`
	PausedAt    int64  `json:"pausedAt"`
	ResumedAt   int64  `json:"resumedAt,omitempty"`
	SatAAckAt   int64  `json:"satA_ackAt,omitempty"`
	SatBAckAt   int64  `json:"satB_ackAt,omitempty"`
	Reason      string `json:"reason"`      // SETTLEMENT|BALANCE_DEPLETED|OPERATOR_REQUEST
	InitiatedBy string `json:"initiatedBy"` // operator MSP ID
}

// ── Top-Up Request ────────────────────────────────────────────────────────────

type TopUpRequest struct {
	ID                string  `json:"topUpId"`
	OperatorChannelID string  `json:"operatorChannelId"`
	RequestedBy       string  `json:"requestedBy"`
	AmountA           float64 `json:"amountA"`
	AmountB           float64 `json:"amountB"`
	ConfirmedBy       string  `json:"confirmedBy,omitempty"`
	Status            string  `json:"status"` // PENDING|CONFIRMED|EXPIRED
	RequestedAt       int64   `json:"requestedAt"`
	ExpiresAt         int64   `json:"expiresAt"`
}

// ── Settlement Notification ───────────────────────────────────────────────────

type SettlementNotification struct {
	ID              string `json:"notifId"`
	TargetOperator  string `json:"targetOperator"`
	TargetSatellite string `json:"targetSatellite,omitempty"`
	Type            string `json:"type"`
	Payload         string `json:"payload"` // JSON string
	CreatedAt       int64  `json:"createdAt"`
	Acknowledged    bool   `json:"acknowledged"`
	AcknowledgedAt  int64  `json:"acknowledgedAt,omitempty"`
}

// ── Satellite Key Record ──────────────────────────────────────────────────────

type SatKey struct {
	SatelliteID  string `json:"satelliteId"`
	OperatorID   string `json:"operatorId"`
	PubKeyPEM    string `json:"pubKeyPEM"`
	IsRevoked    bool   `json:"isRevoked"`
	RegisteredAt int64  `json:"registeredAt"`
	RevokedAt    int64  `json:"revokedAt"` // always serialised; 0 means not revoked
}

// ── Chaincode event names ─────────────────────────────────────────────────────

const (
	EventChannelOpened        = "ChannelOpened"
	EventSettlementInitiated  = "SettlementInitiated"
	EventSettlementChallenged = "SettlementChallenged"
	EventSettlementFinalized  = "SettlementFinalized"
	EventDisputeResolved      = "DisputeResolved"
	EventPenaltyApplied       = "PenaltyApplied"
	EventTopUpRequested       = "TopUpRequested"
	EventTopUpConfirmed       = "TopUpConfirmed"
	EventTopUpExpired         = "TopUpExpired"
	EventISLPaused            = "ISLPaused"
	EventISLResumed           = "ISLResumed"
	EventBalanceReset         = "BalanceReset"
	EventKeyRegistered        = "KeyRegistered"
	EventKeyRevoked           = "KeyRevoked"
)

// ── Bulk Initialization Inputs ────────────────────────────────────────────────

type OperatorChannelInit struct {
	OperatorA string  `json:"operatorA"`
	OperatorB string  `json:"operatorB"`
	BalanceA  float64 `json:"balanceA"`
	BalanceB  float64 `json:"balanceB"`
	ReserveA  float64 `json:"reserveA"`
	ReserveB  float64 `json:"reserveB"`
}

type SatKeyInit struct {
	OperatorID  string `json:"operatorId"`
	SatelliteID string `json:"satelliteId"`
	PubKeyPEM   string `json:"pubKeyPEM"`
}

// ── Protocol constants ────────────────────────────────────────────────────────

const (
	TChallengeWindowSec int64 = 172800 // 48 hours
	TTopupConfirmSec    int64 = 86400  // 24 hours
)
