package main

// ── Operator-Level Channel ───────────────────────────────────────────────────

type OperatorChannel struct {
	ID               string  `json:"channelId"`
	OperatorA        string  `json:"operatorA"`
	OperatorB        string  `json:"operatorB"`
	BalanceA         float64 `json:"balanceA"`
	BalanceB         float64 `json:"balanceB"`
	PenaltyReserveA  float64 `json:"penaltyReserveA"`
	PenaltyReserveB  float64 `json:"penaltyReserveB"`
	Status           string  `json:"status"` // OPEN|SETTLING|DISPUTED|CLOSED
	OpenedAt         int64   `json:"openedAt"`
	ClosedAt         *int64  `json:"closedAt,omitempty"`
}

// ── Satellite-Pair Sub-Channel ───────────────────────────────────────────────

type SatChannel struct {
	ID                string             `json:"satChannelId"`
	OperatorChannelID string             `json:"operatorChannelId"`
	SatA              string             `json:"satelliteA"`
	SatB              string             `json:"satelliteB"`
	SeqNum            int64              `json:"currentSeqNum"`
	BalanceA          float64            `json:"balanceA"`
	BalanceB          float64            `json:"balanceB"`
	HashHeadA         string             `json:"hashChainHeadA"`
	HashHeadB         string             `json:"hashChainHeadB"`
	LastUpdated       int64              `json:"lastUpdated"`
	Status            string             `json:"status"` // ACTIVE|PAUSED|PENDING_SETTLEMENT|SETTLED
	OutOfServiceLog   []OutOfServiceEntry `json:"outOfServiceLog"`
	SettlementSubmittedAt *int64         `json:"settlementSubmittedAt,omitempty"`
	SubmittedProof    *BalanceProof      `json:"submittedProof,omitempty"`
}

// ── Balance Proof ────────────────────────────────────────────────────────────

type BalanceProof struct {
	ChannelID   string  `json:"satChannelId"`
	SeqNum      int64   `json:"seqNum"`
	BalanceA    float64 `json:"balanceA"`
	BalanceB    float64 `json:"balanceB"`
	HashHeadA   string  `json:"hashChainHeadA"`
	HashHeadB   string  `json:"hashChainHeadB"`
	SigA        string  `json:"sigA"`        // DER hex ECDSA P-256
	SigB        string  `json:"sigB"`
	SubmittedBy string  `json:"submittedBy"` // operator MSP ID
	SubmittedAt int64   `json:"submittedAt"`
}

// ── Dispute Record ───────────────────────────────────────────────────────────

type DisputeRecord struct {
	ID           string        `json:"disputeId"`
	ChannelID    string        `json:"satChannelId"`
	ClaimedProof BalanceProof  `json:"claimedProof"`
	CounterProof *BalanceProof `json:"counterProof,omitempty"`
	Status       string        `json:"status"` // OPEN|RESOLVED_PENALTY|RESOLVED_VALID
	OpenedAt     int64         `json:"openedAt"`
	ResolvedAt   *int64        `json:"resolvedAt,omitempty"`
	PenaltyPaid  bool          `json:"penaltyPaid"`
}

// ── Out-of-Service Log Entry ─────────────────────────────────────────────────

type OutOfServiceEntry struct {
	ChannelID   string `json:"satChannelId"`
	PausedAt    int64  `json:"pausedAt"`
	ResumedAt   *int64 `json:"resumedAt,omitempty"`
	SatAAckAt   *int64 `json:"satA_ackAt,omitempty"`
	SatBAckAt   *int64 `json:"satB_ackAt,omitempty"`
	Reason      string `json:"reason"`      // SETTLEMENT|BALANCE_DEPLETED|OPERATOR_REQUEST
	InitiatedBy string `json:"initiatedBy"` // operator MSP ID
}

// ── Top-Up Request ───────────────────────────────────────────────────────────

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

// ── Settlement Notification ──────────────────────────────────────────────────

type SettlementNotification struct {
	ID              string `json:"notifId"`
	TargetOperator  string `json:"targetOperator"`
	TargetSatellite string `json:"targetSatellite,omitempty"`
	Type            string `json:"type"`    // SETTLEMENT_STARTED|SETTLEMENT_COMPLETE|...
	Payload         string `json:"payload"` // JSON string
	CreatedAt       int64  `json:"createdAt"`
	Acknowledged    bool   `json:"acknowledged"`
	AcknowledgedAt  *int64 `json:"acknowledgedAt,omitempty"`
}

// ── Satellite Key Record ─────────────────────────────────────────────────────

type SatKey struct {
	SatelliteID  string `json:"satelliteId"`
	OperatorID   string `json:"operatorId"`
	PubKeyPEM    string `json:"pubKeyPEM"`
	IsRevoked    bool   `json:"isRevoked"`
	RegisteredAt int64  `json:"registeredAt"`
	RevokedAt    *int64 `json:"revokedAt,omitempty"`
}

// ── Chaincode event names ─────────────────────────────────────────────────────

const (
	EventChannelOpened         = "ChannelOpened"
	EventSettlementInitiated   = "SettlementInitiated"
	EventSettlementChallenged  = "SettlementChallenged"
	EventSettlementFinalized   = "SettlementFinalized"
	EventDisputeOpened         = "DisputeOpened"
	EventDisputeResolved       = "DisputeResolved"
	EventPenaltyApplied        = "PenaltyApplied"
	EventTopUpRequested        = "TopUpRequested"
	EventTopUpConfirmed        = "TopUpConfirmed"
	EventTopUpExpired          = "TopUpExpired"
	EventISLPaused             = "ISLPaused"
	EventISLResumed            = "ISLResumed"
	EventKeyRegistered         = "KeyRegistered"
	EventKeyRevoked            = "KeyRevoked"
)

// ── Constants ─────────────────────────────────────────────────────────────────

const (
	TChallengeWindowSec int64 = 172800 // 48 hours
	TTopupConfirmSec    int64 = 86400  // 24 hours
)
