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
)

// VerifyECDSA verifies an ECDSA P-256 / SHA-256 signature.
//
//   pubKeyPEM:   PEM-encoded EC public key (SubjectPublicKeyInfo).
//   messageJSON: the signed data (already canonicalised).
//   sigHex:      DER-encoded signature as lowercase hex string.
func VerifyECDSA(pubKeyPEM, messageJSON, sigHex string) bool {
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
	// Parse DER-encoded (r, s) pair
	var sig struct{ R, S *big.Int }
	if _, err := asn1.Unmarshal(sigDER, &sig); err != nil {
		return false
	}
	digest := sha256.Sum256([]byte(messageJSON))
	return ecdsa.Verify(ecPub, digest[:], sig.R, sig.S)
}

// CanonicalJSON serialises v to JSON with sorted keys (for deterministic signatures).
func CanonicalJSON(v interface{}) ([]byte, error) {
	// Standard json.Marshal produces alphabetically-sorted struct fields.
	// For map types we need explicit key sorting.
	raw, err := json.Marshal(v)
	if err != nil {
		return nil, err
	}
	// Round-trip through a map to ensure consistent key ordering
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

// BalanceProofCanonicalJSON returns the canonical JSON of a BalanceProof
// excluding the signature fields (the payload that is signed).
func BalanceProofCanonicalJSON(proof BalanceProof) (string, error) {
	payload := map[string]interface{}{
		"satChannelId":    proof.ChannelID,
		"seqNum":          proof.SeqNum,
		"balanceA":        proof.BalanceA,
		"balanceB":        proof.BalanceB,
		"hashChainHeadA": proof.HashHeadA,
		"hashChainHeadB": proof.HashHeadB,
	}
	b, err := CanonicalJSON(payload)
	if err != nil {
		return "", err
	}
	return string(b), nil
}

// VerifyBalanceProof verifies both ECDSA signatures on a BalanceProof.
func VerifyBalanceProof(proof BalanceProof, pubKeyPEMA, pubKeyPEMB string) error {
	canonical, err := BalanceProofCanonicalJSON(proof)
	if err != nil {
		return fmt.Errorf("canonicalisation failed: %w", err)
	}
	if proof.SigA != "" && !VerifyECDSA(pubKeyPEMA, canonical, proof.SigA) {
		return fmt.Errorf("invalid SigA")
	}
	if proof.SigB != "" && !VerifyECDSA(pubKeyPEMB, canonical, proof.SigB) {
		return fmt.Errorf("invalid SigB")
	}
	return nil
}

