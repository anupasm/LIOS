package main

import (
	"crypto/ecdsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/asn1"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"math/big"
	"sort"
	"strconv"
	"strings"
)

// VerifyECDSA verifies an ECDSA P-256 / SHA-256 signature.
//
//	pubKeyPEM: PEM-encoded EC public key (SubjectPublicKeyInfo).
//	message:   the signed data (already serialised to bytes).
//	sigHex:    DER-encoded signature as lowercase hex string.
func VerifyECDSA(pubKeyPEM, message, sigHex string) bool {
	block, _ := pem.Decode([]byte(pubKeyPEM))
	if block == nil {
		return false
	}
	pub, err := x509.ParsePKIXPublicKey(block.Bytes)
	if err != nil {
		return false
	}
	ecPub, ok := pub.(*ecdsa.PublicKey)
	if !ok {
		return false
	}
	sigDER, err := hex.DecodeString(sigHex)
	if err != nil {
		return false
	}
	var sig struct{ R, S *big.Int }
	if _, err := asn1.Unmarshal(sigDER, &sig); err != nil {
		return false
	}
	digest := sha256.Sum256([]byte(message))
	return ecdsa.Verify(ecPub, digest[:], sig.R, sig.S)
}

// BalanceProofSigningPayload reconstructs the canonical JSON that the Python
// off-chain protocol signs via BalanceProof.signing_payload():
//
//	json.dumps({
//	    "balance_a_kb": ..., "balance_b_kb": ..., "channel_id": ..., "seq_num": ...
//	}, sort_keys=True, separators=(",", ":"))
//
// Keys are in ascending alphabetical order (matching Python sort_keys=True).
// Floats use Python repr: integer-valued floats always include ".0" (e.g. 1000.0).
func BalanceProofSigningPayload(proof BalanceProof) string {
	return fmt.Sprintf(
		`{"balance_a_kb":%s,"balance_b_kb":%s,"channel_id":%s,"seq_num":%d}`,
		pyFloat(proof.BalanceA),
		pyFloat(proof.BalanceB),
		jsonStr(proof.ChannelID),
		proof.SeqNum,
	)
}

// pyFloat formats a float64 the way Python's json.dumps does: integer-valued
// floats always include a decimal point (e.g. 1000 → "1000.0").
func pyFloat(f float64) string {
	s := strconv.FormatFloat(f, 'f', -1, 64)
	if !strings.Contains(s, ".") {
		s += ".0"
	}
	return s
}

// jsonStr JSON-encodes a string (adding quotes and escaping special characters).
func jsonStr(s string) string {
	b, _ := json.Marshal(s)
	return string(b)
}

// VerifyBalanceProof verifies the ECDSA signatures on a BalanceProof.
// Sig fields present in the proof are verified; absent sigs are accepted.
func VerifyBalanceProof(proof BalanceProof, pubKeyPEMA, pubKeyPEMB string) error {
	payload := BalanceProofSigningPayload(proof)
	if proof.SigA != "" && !VerifyECDSA(pubKeyPEMA, payload, proof.SigA) {
		return fmt.Errorf("invalid SigA")
	}
	if proof.SigB != "" && !VerifyECDSA(pubKeyPEMB, payload, proof.SigB) {
		return fmt.Errorf("invalid SigB")
	}
	return nil
}

// parseSatPairID splits "satA__satB" into ("satA", "satB").
func parseSatPairID(satPairID string) (satA, satB string, err error) {
	parts := strings.SplitN(satPairID, "__", 2)
	if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
		return "", "", fmt.Errorf("invalid satPairID %q: expected satA__satB", satPairID)
	}
	return parts[0], parts[1], nil
}

// CanonicalJSON serialises v to JSON with sorted keys (for deterministic output).
func CanonicalJSON(v interface{}) ([]byte, error) {
	raw, err := json.Marshal(v)
	if err != nil {
		return nil, err
	}
	var m interface{}
	if err := json.Unmarshal(raw, &m); err != nil {
		return nil, err
	}
	return sortedJSON(m)
}

func sortedJSON(v interface{}) ([]byte, error) {
	switch val := v.(type) {
	case map[string]interface{}:
		keys := make([]string, 0, len(val))
		for k := range val {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		buf := []byte{'{'}
		for i, k := range keys {
			kb, _ := json.Marshal(k)
			vb, err := sortedJSON(val[k])
			if err != nil {
				return nil, err
			}
			if i > 0 {
				buf = append(buf, ',')
			}
			buf = append(buf, kb...)
			buf = append(buf, ':')
			buf = append(buf, vb...)
		}
		return append(buf, '}'), nil
	case []interface{}:
		buf := []byte{'['}
		for i, elem := range val {
			eb, err := sortedJSON(elem)
			if err != nil {
				return nil, err
			}
			if i > 0 {
				buf = append(buf, ',')
			}
			buf = append(buf, eb...)
		}
		return append(buf, ']'), nil
	default:
		return json.Marshal(val)
	}
}
