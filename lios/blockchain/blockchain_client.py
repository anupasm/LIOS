"""
Real Hyperledger Fabric client — drop-in replacement for FabricMock.

All chaincode calls use ``docker exec <cli-container> peer chaincode invoke/query``
so there are zero extra Python dependencies and no TLS cert wrangling in Python.

Prerequisites
-------------
1. Start the network and generate the config::

       ./lios/evaluation/start_network.sh

Config
------
Reads ``lios/blockchain/network_config.json`` (written by start_network.sh).

.. code-block:: json

   {
     "channel"         : "isl-settlement",
     "chaincode"       : "isl-settlement",
     "default_org"     : "OperatorA",
     "org_name_map"    : {"alpha": "OperatorA", "beta": "OperatorB", "gamma": "OperatorC"},
     "org_meta"        : {
       "OperatorA": {
         "cli"           : "cli.operator-a.lios.example.com",
         "peer0_address" : "peer0.operator-a.lios.example.com:7041",
         "peer0_tls_cert": "/var/hyperledger/cli/crypto-peer/peer0.operator-a.lios.example.com/tls/ca.crt"
       }, ...
     },
     "orderer_address" : "orderer0.orderer-group.orderer.lios.example.com:7030",
     "orderer_tls_cert": "/var/hyperledger/cli/crypto-orderer/tlsca.orderer.lios.example.com-cert.pem"
   }
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)
log.setLevel(logging.WARNING)


class FabricConnectionError(RuntimeError):
    """Raised when the network config is missing or containers are unreachable."""


class FabricClient:
    """
    Fabric client that uses ``docker exec … peer chaincode invoke/query``.
    Drop-in replacement for ``FabricMock``.

    Invoke transactions collect endorsements from all peer orgs (satisfying
    the ``AND(OperatorAMSP, OperatorBMSP, OperatorCMSP)`` endorsement policy)
    by passing ``--peerAddresses`` for every org's peer0.

    Satellite-pair channels are no longer pre-registered on-chain.
    SubmitCoSignedSettlement / InitiateSettlement create the SettlementRecord
    lazily on first call, removing the need for a separate EnsureSatChannel tx.
    """

    _DEFAULT_CFG = Path(__file__).parent / "network_config.json"

    def __init__(self, config_path: Optional[Path] = None) -> None:
        cfg_path = Path(config_path) if config_path else self._DEFAULT_CFG
        if not cfg_path.exists():
            raise FabricConnectionError(
                f"network_config.json not found at {cfg_path}\n"
                "Run ./lios/evaluation/start_network.sh first."
            )

        with cfg_path.open() as fh:
            cfg = json.load(fh)

        self._channel:          str             = cfg["channel"]
        self._cc:               str             = cfg["chaincode"]
        self._org_map:          Dict[str, str]  = cfg.get("org_name_map", {})
        self._default_org:      str             = cfg.get("default_org", "OperatorA")
        self._org_meta:         Dict[str, dict] = cfg.get("org_meta", {})
        self._orderer_address:  str             = cfg.get("orderer_address", "")
        self._orderer_tls_cert: str             = cfg.get("orderer_tls_cert", "")

        if not self._org_meta:
            raise FabricConnectionError(
                "org_meta missing from network_config.json — "
                "re-run ./lios/evaluation/start_network.sh to regenerate it."
            )

        self._notifications: List[dict] = []
        self.commit_latency_sec: float = 0.0

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _to_org(self, operator_id: Optional[str]) -> str:
        """Map simulation operator ID → Fablo org name (e.g. alpha → OperatorA)."""
        if operator_id and operator_id in self._org_map:
            return self._org_map[operator_id]
        return self._default_org

    def _cli(self, org: str) -> str:
        return self._org_meta.get(org, {}).get("cli", f"cli.{org.lower()}.lios.example.com")

    def _invoke(
        self,
        fn: str,
        args: List[str],
        operator_id: Optional[str] = None,
    ) -> bool:
        """Submit a read-write transaction via docker exec … peer chaincode invoke.

        Sends the proposal to peer0 of every org so the AND endorsement policy
        is always satisfied.  Returns True on success, False on error.
        """
        org     = self._to_org(operator_id)
        cli     = self._cli(org)
        payload = json.dumps({"Args": [fn] + args})

        peer_flags: List[str] = []
        for meta in self._org_meta.values():
            peer_flags += [
                "--peerAddresses",   meta["peer0_address"],
                "--tlsRootCertFiles", meta["peer0_tls_cert"],
            ]

        cmd = [
            "docker", "exec", "-i", cli,
            "peer", "chaincode", "invoke",
            "-C", self._channel,
            "-n", self._cc,
            "-c", payload,
            "-o", self._orderer_address,
            "--tls",
            "--cafile", self._orderer_tls_cert,
            "--waitForEvent",           # block until tx is committed to a block
            "--waitForEventTimeout", "30s",
        ] + peer_flags

        log.info("INVOKE  fn=%s  org=%s  args=%s", fn, org, args)

        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60,
            )
        except subprocess.TimeoutExpired:
            log.error("invoke %s timed out after 60 s", fn)
            self.commit_latency_sec = 0.0
            return False
        except OSError as exc:
            log.error("invoke %s OS error: %s", fn, exc)
            self.commit_latency_sec = 0.0
            return False

        self.commit_latency_sec = time.time() - t0
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout).strip()
            log.warning("invoke %s [rc=%d]: %s", fn, proc.returncode, err)
            return False

        log.info("INVOKE  fn=%s  OK  %s", fn, (proc.stderr.strip() or proc.stdout.strip()))
        return True

    def _query(
        self,
        fn: str,
        args: List[str],
        operator_id: Optional[str] = None,
    ) -> Optional[str]:
        """Run a read-only query via docker exec … peer chaincode query."""
        org     = self._to_org(operator_id)
        cli     = self._cli(org)
        payload = json.dumps({"Args": [fn] + args})

        cmd = [
            "docker", "exec", "-i", cli,
            "peer", "chaincode", "query",
            "-C", self._channel,
            "-n", self._cc,
            "-c", payload,
        ]

        log.info("QUERY   fn=%s  org=%s  args=%s", fn, org, args)

        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30,
            )
        except Exception as exc:
            log.error("query %s error: %s", fn, exc)
            return None

        if proc.returncode != 0:
            log.warning("query %s [rc=%d]: %s", fn, proc.returncode, proc.stderr.strip())
            return None

        result = proc.stdout.strip()
        log.info("QUERY   fn=%s  OK  %s", fn, result)
        return result

    # ── FabricMock-compatible public interface ─────────────────────────────────

    # Each satellite entry is ~350 bytes of JSON (PEM key + IDs).  The OS
    # execve arg-list limit is typically 128 KB, so cap each invoke at 50 sats
    # to stay well under that ceiling (50 × 350 B ≈ 17 KB per call).
    _SAT_BATCH_SIZE = 50

    def bulk_init(
        self,
        operator_pairs: list,
        satellite_keys: list,
    ) -> None:
        """Register all satellite keys and open operator channels via InitLedger.

        operator_pairs: [{"op_a", "op_b", "bal_a", "bal_b", "reserve"}, ...]
        satellite_keys: [{"operator_id", "satellite_id", "pub_key_pem"}, ...]

        Satellite keys are sent in batches of _SAT_BATCH_SIZE to stay within
        the OS execve argument-list size limit.  Operator channels go in the
        first batch (they are few and small).  InitLedger is idempotent so
        repeated calls for the same keys or channels are safe.
        """
        batches = [
            satellite_keys[i: i + self._SAT_BATCH_SIZE]
            for i in range(0, max(len(satellite_keys), 1), self._SAT_BATCH_SIZE)
        ]
        n_batches = len(batches)
        log.info(
            "InitLedger: %d operator channels + %d satellite keys in %d batch(es)",
            len(operator_pairs), len(satellite_keys), n_batches,
        )

        for batch_idx, sat_batch in enumerate(batches):
            # Operator channels only in the first batch; idempotent skip on repeats.
            ops = operator_pairs if batch_idx == 0 else []
            operators_json = json.dumps([
                {
                    "operatorA": p["op_a"],
                    "operatorB": p["op_b"],
                    "balanceA":  p["bal_a"],
                    "balanceB":  p["bal_b"],
                    "reserveA":  p["reserve"],
                    "reserveB":  p["reserve"],
                }
                for p in ops
            ])
            satellites_json = json.dumps([
                {
                    "operatorId":  s["operator_id"],
                    "satelliteId": s["satellite_id"],
                    "pubKeyPEM":   s["pub_key_pem"],
                }
                for s in sat_batch
            ])
            log.info(
                "InitLedger batch %d/%d: %d channels, %d sats",
                batch_idx + 1, n_batches, len(ops), len(sat_batch),
            )
            ok = self._invoke("InitLedger", [operators_json, satellites_json])
            if not ok:
                raise RuntimeError(
                    f"InitLedger batch {batch_idx + 1}/{n_batches} failed — "
                    "check Fabric network logs. "
                    "Re-run ./lios/evaluation/start_network.sh --reset to clear state."
                )

        log.info("InitLedger complete: all batches committed.")

    def open_operator_channel(
        self,
        op_a: str,
        op_b: str,
        bal_a: float,
        bal_b: float,
        reserve: float,
    ) -> str:
        """Idempotently open a bilateral operator channel on-chain."""
        ch_id = f"{op_a}_{op_b}_ch"
        ok = self._invoke(
            "EnsureOperatorChannel",
            [op_a, op_b, str(bal_a), str(bal_b), str(reserve), str(reserve)],
            operator_id=op_a,
        )
        if not ok:
            raise RuntimeError(
                f"Failed to create operator channel {ch_id} on-chain — "
                "check Fabric network logs for details."
            )
        log.info("Operator channel %s ensured on-chain", ch_id)
        return ch_id

    def register_satellite_key(
        self,
        operator_id: str,
        satellite_id: str,
        pub_key_pem: str,
    ) -> None:
        """Register or overwrite a satellite public key on-chain."""
        ok = self._invoke(
            "UpsertSatelliteKey",
            [operator_id, satellite_id, pub_key_pem],
            operator_id=operator_id,
        )
        if ok:
            log.info("Satellite key registered on-chain: %s", satellite_id)
        else:
            log.warning(
                "UpsertSatelliteKey FAILED for satellite %s (op=%s). "
                "SubmitBalanceReset / SubmitCoSignedSettlement will fail later. "
                "Check Fabric connectivity and endorsement policy.",
                satellite_id, operator_id,
            )

    def initiate_settlement(
        self,
        sat_channel_id: str,
        proof: Any,
        submitted_by: str,
        t: float,
    ) -> dict:
        """Submit a BalanceProof on-chain.  Returns a status dict matching FabricMock.

        Routes to SubmitCoSignedSettlement (both sigs present → MUTUAL_FINALIZED
        immediately, no Tch window) or InitiateSettlement (one sig → NEW_PENDING,
        Tch challenge window started).  Returns a dict with 'status' and 'tx_id'
        so the caller can apply the same logic as it does for FabricMock results.
        """
        op_channel_id = self._op_channel_id_from_pair(sat_channel_id)
        proof_json = self._proof_to_chaincode_json(proof, submitted_by)
        tx_id = str(uuid.uuid4())

        cosigned = bool(proof.sig_a) and bool(proof.sig_b)
        if cosigned:
            fn         = "SubmitCoSignedSettlement"
            ok_status  = "MUTUAL_FINALIZED"
            event_type = "SETTLEMENT_FINALIZED"
        else:
            fn         = "InitiateSettlement"
            ok_status  = "NEW_PENDING"
            event_type = "SETTLEMENT_INITIATED"

        ok = self._invoke(
            fn,
            [sat_channel_id, op_channel_id, proof_json],
            operator_id=submitted_by,
        )
        if ok:
            log.info(
                "%s on-chain: pair=%s seq=%d tx=%s",
                fn, sat_channel_id, proof.seq_num, tx_id,
            )
            self._push_notification(
                event_type, {"satChannelId": sat_channel_id}, t,
            )
            return {"status": ok_status, "tx_id": tx_id}

        # Chaincode rejected — likely stale seq_num (rollback attempt).
        log.warning(
            "%s rejected on-chain: pair=%s seq=%d",
            fn, sat_channel_id, proof.seq_num,
        )
        return {"status": "REJECTED_STALE", "tx_id": ""}

    def submit_balance_reset(
        self,
        sat_channel_id: str,
        proof: Any,
        submitted_by: str,
        t: float,
    ) -> str:
        """Submit a co-signed balance reset to the chaincode.

        Both satellite signatures must be present.  The chaincode verifies them,
        debits the net transfer from the operator channel, and emits BalanceReset.

        If the peer GS already committed this reset (same seqNum), the chaincode
        returns "proof seqNum N <= latest settled seqNum N".  Treat this as a
        success: the reset is already on-chain and ISL_RESUME can proceed.
        """
        op_channel_id = self._op_channel_id_from_pair(sat_channel_id)
        proof_json = self._proof_to_chaincode_json(proof, submitted_by)
        tx_id = str(uuid.uuid4())
        ok = self._invoke(
            "SubmitBalanceReset",
            [sat_channel_id, op_channel_id, proof_json],
            operator_id=submitted_by,
        )
        # "proof seqNum X <= latest settled seqNum X" means the peer GS already
        # committed this exact reset — treat as success (idempotent).
        if not ok:
            result = self._query("GetSettlementRecord", [sat_channel_id])
            if result and f'"latestSeqNum":{proof.seq_num}' in result.replace(" ", ""):
                log.info(
                    "BalanceReset already on-chain (peer GS committed first): "
                    "pair=%s seq=%d", sat_channel_id, proof.seq_num,
                )
                ok = True
        if ok:
            log.info(
                "BalanceReset on-chain: pair=%s seq=%d tx=%s",
                sat_channel_id, proof.seq_num, tx_id,
            )
            self._push_notification(
                "BALANCE_RESET", {"satChannelId": sat_channel_id}, t,
            )
        return tx_id

    def challenge_settlement(
        self,
        sat_channel_id: str,
        counter_proof: Any,
        t: float,
        submitted_by: str = "",
    ) -> bool:
        """Submit a newer BalanceProof to challenge a pending dispute."""
        proof_json = self._proof_to_chaincode_json(counter_proof, submitted_by)
        ok = self._invoke("ChallengeSettlement", [sat_channel_id, proof_json],
                          operator_id=submitted_by or None)
        if ok:
            self._push_notification(
                "SETTLEMENT_CHALLENGED", {"satChannelId": sat_channel_id}, t,
            )
        return ok

    def finalize_settlement(self, sat_channel_id: str, t: float) -> bool:  # noqa: ARG002
        """No-op: co-signed proofs are finalised atomically in initiate_settlement."""
        return True

    def get_pending_settlement(self, sat_channel_id: str) -> dict:
        """Query the current settlement record from the ledger."""
        import json as _json
        result = self._query("GetSettlementRecord", [sat_channel_id])
        if not result:
            return {}
        try:
            return _json.loads(result)
        except Exception:
            return {}

    def get_penalty_events(self) -> list:
        """Return penalty events detected by the on-chain challenge mechanism."""
        return []

    def get_attack_detection_stats(self) -> dict:
        """Aggregate detection metrics — not available from the on-chain event hub."""
        return {"total_detected": 0, "detection_latency_sec": {}}

    def verify_satellite_key(self, sat_id: str) -> bool:
        """Query the on-chain satellite key registry and return True if found."""
        result = self._query("GetSatelliteKey", [sat_id])
        return bool(result)

    def get_pending_notifications(self, operator_id: str) -> List[dict]:  # noqa: ARG002
        return [n for n in self._notifications if not n.get("acknowledged")]

    def acknowledge_notification(self, notif_id: str) -> None:
        for n in self._notifications:
            if n["id"] == notif_id:
                n["acknowledged"] = True
                return

    def get_peer_balance_proofs(self, satellite_id: str) -> List[dict]:  # noqa: ARG002
        return []

    # ── Internal helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _op_channel_id_from_pair(sat_pair_id: str) -> str:
        """Derive operator channel ID from satellite pair ID (satA__satB format).

        Satellite IDs are expected to start with the operator prefix, e.g.
        "alpha-1__beta-2" → operators "alpha" and "beta" → "alpha_beta_ch".
        """
        sat_a, sat_b = sat_pair_id.split("__", 1)
        op_a = sat_a.split("-")[0]
        op_b = sat_b.split("-")[0]
        return f"{op_a}_{op_b}_ch"

    @staticmethod
    def _proof_to_chaincode_json(proof: Any, submitted_by: str) -> str:
        return json.dumps({
            "satChannelId": proof.channel_id,
            "seqNum":       proof.seq_num,
            "balanceA":     proof.balance_a_kb,
            "balanceB":     proof.balance_b_kb,
            "sigA":         proof.sig_a.hex() if proof.sig_a else "",
            "sigB":         proof.sig_b.hex() if proof.sig_b else "",
            "submittedBy":  submitted_by,
            "submittedAt":  0,
        })

    def _push_notification(self, notif_type: str, payload: dict, t: float) -> None:
        self._notifications.append({
            "id":           str(uuid.uuid4()),
            "type":         notif_type,
            "payload":      payload,
            "created_at":   t,
            "acknowledged": False,
        })
