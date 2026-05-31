package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"testing"

	"github.com/stretchr/testify/require"
)

// ── helpers shared across both test files ─────────────────────────────────────

// genKey generates an ECDSA P-256 key pair; returns private key + PEM-encoded
// SubjectPublicKeyInfo (the format stored on-chain and used by VerifyECDSA).
func genKey(t *testing.T) (*ecdsa.PrivateKey, string) {
	t.Helper()
	priv, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	require.NoError(t, err)
	pubDER, err := x509.MarshalPKIXPublicKey(&priv.PublicKey)
	require.NoError(t, err)
	pubPEM := pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: pubDER})
	return priv, string(pubPEM)
}

// signPayload signs `payload` with `priv` and returns the hex DER signature,
// exactly as the Python off-chain protocol produces with ECDSA(hashes.SHA256()).
func signPayload(t *testing.T, priv *ecdsa.PrivateKey, payload string) string {
	t.Helper()
	digest := sha256.Sum256([]byte(payload))
	sig, err := ecdsa.SignASN1(rand.Reader, priv, digest[:])
	require.NoError(t, err)
	return hex.EncodeToString(sig)
}

// makeProof builds a BalanceProof and optionally co-signs it with privA / privB
// (pass nil to leave that sig empty).
func makeProof(
	chID string, seq int64, balA, balB float64,
	privA, privB *ecdsa.PrivateKey,
) BalanceProof {
	p := BalanceProof{
		ChannelID: chID,
		SeqNum:    seq,
		BalanceA:  balA,
		BalanceB:  balB,
	}
	payload := BalanceProofSigningPayload(p)
	if privA != nil {
		digest := sha256.Sum256([]byte(payload))
		sig, _ := ecdsa.SignASN1(rand.Reader, privA, digest[:])
		p.SigA = hex.EncodeToString(sig)
	}
	if privB != nil {
		digest := sha256.Sum256([]byte(payload))
		sig, _ := ecdsa.SignASN1(rand.Reader, privB, digest[:])
		p.SigB = hex.EncodeToString(sig)
	}
	return p
}

// proofJSON marshals a BalanceProof to the JSON string expected by chaincode.
func proofJSON(t *testing.T, p BalanceProof) string {
	t.Helper()
	b, err := json.Marshal(p)
	require.NoError(t, err)
	return string(b)
}

// ── utils unit tests ──────────────────────────────────────────────────────────

func TestPyFloat(t *testing.T) {
	cases := []struct {
		in   float64
		want string
	}{
		{0.0, "0.0"},
		{1.0, "1.0"},
		{1000.0, "1000.0"},
		{1024.0, "1024.0"},
		{-512.0, "-512.0"},
		{1000.5, "1000.5"},
		{0.123456789, "0.123456789"},
		{-1.5, "-1.5"},
	}
	for _, tc := range cases {
		require.Equal(t, tc.want, pyFloat(tc.in), "pyFloat(%v)", tc.in)
	}
}

func TestBalanceProofSigningPayload(t *testing.T) {
	p := BalanceProof{
		ChannelID: "alpha-1__beta-1",
		SeqNum:    5,
		BalanceA:  1000.0,
		BalanceB:  500.0,
	}
	got := BalanceProofSigningPayload(p)
	// Keys must be alphabetically sorted; floats must use Python repr (.0 suffix).
	want := `{"balance_a_kb":1000.0,"balance_b_kb":500.0,"channel_id":"alpha-1__beta-1","seq_num":5}`
	require.Equal(t, want, got)
}

func TestBalanceProofSigningPayload_FractionalBalance(t *testing.T) {
	p := BalanceProof{
		ChannelID: "x__y",
		SeqNum:    1,
		BalanceA:  768.5,
		BalanceB:  255.5,
	}
	got := BalanceProofSigningPayload(p)
	want := `{"balance_a_kb":768.5,"balance_b_kb":255.5,"channel_id":"x__y","seq_num":1}`
	require.Equal(t, want, got)
}

func TestVerifyECDSA_Valid(t *testing.T) {
	priv, pubPEM := genKey(t)
	payload := `{"hello":"world"}`
	sigHex := signPayload(t, priv, payload)
	require.True(t, VerifyECDSA(pubPEM, payload, sigHex))
}

func TestVerifyECDSA_WrongKey(t *testing.T) {
	priv, _ := genKey(t)
	_, otherPEM := genKey(t)
	payload := `{"hello":"world"}`
	sigHex := signPayload(t, priv, payload)
	require.False(t, VerifyECDSA(otherPEM, payload, sigHex))
}

func TestVerifyECDSA_TamperedPayload(t *testing.T) {
	priv, pubPEM := genKey(t)
	payload := `{"amount":100.0}`
	sigHex := signPayload(t, priv, payload)
	require.False(t, VerifyECDSA(pubPEM, `{"amount":200.0}`, sigHex))
}

func TestVerifyECDSA_InvalidPEM(t *testing.T) {
	require.False(t, VerifyECDSA("not-a-pem", "payload", "aabb"))
}

func TestVerifyECDSA_InvalidSigHex(t *testing.T) {
	_, pubPEM := genKey(t)
	require.False(t, VerifyECDSA(pubPEM, "payload", "not-hex!!"))
}

func TestVerifyBalanceProof_BothSigsValid(t *testing.T) {
	privA, pemA := genKey(t)
	privB, pemB := genKey(t)
	p := makeProof("ch1", 3, 600.0, 400.0, privA, privB)
	require.NoError(t, VerifyBalanceProof(p, pemA, pemB))
}

func TestVerifyBalanceProof_OnlySigA(t *testing.T) {
	privA, pemA := genKey(t)
	_, pemB := genKey(t)
	p := makeProof("ch1", 1, 500.0, 500.0, privA, nil)
	require.NoError(t, VerifyBalanceProof(p, pemA, pemB))
}

func TestVerifyBalanceProof_BadSigA(t *testing.T) {
	privA, _ := genKey(t)
	_, pemA2 := genKey(t) // wrong public key for sigA
	_, pemB := genKey(t)
	p := makeProof("ch1", 1, 500.0, 500.0, privA, nil)
	require.Error(t, VerifyBalanceProof(p, pemA2, pemB))
}

func TestVerifyBalanceProof_NoSigs(t *testing.T) {
	_, pemA := genKey(t)
	_, pemB := genKey(t)
	p := BalanceProof{ChannelID: "ch", SeqNum: 1, BalanceA: 500.0, BalanceB: 500.0}
	require.NoError(t, VerifyBalanceProof(p, pemA, pemB))
}

func TestCanonicalJSON_SortsKeys(t *testing.T) {
	m := map[string]interface{}{"z": 1, "a": 2, "m": 3}
	b, err := CanonicalJSON(m)
	require.NoError(t, err)
	require.Equal(t, `{"a":2,"m":3,"z":1}`, string(b))
}

func TestCanonicalJSON_Nested(t *testing.T) {
	m := map[string]interface{}{
		"outer": map[string]interface{}{"z": "val", "a": "val2"},
	}
	b, err := CanonicalJSON(m)
	require.NoError(t, err)
	require.Equal(t, `{"outer":{"a":"val2","z":"val"}}`, string(b))
}
