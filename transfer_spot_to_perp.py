"""
Transfer USDC from Spot to Perp on Hyperliquid Testnet.
Uses ccxt without load_markets (bypasses testnet bug).

Usage: python transfer_spot_to_perp.py
"""
import ccxt
from dotenv import load_dotenv
import os
import requests
import json

load_dotenv()

PRIVATE_KEY = os.getenv("HL_PRIVATE_KEY")
WALLET_ADDRESS = os.getenv("HL_WALLET_ADDRESS")
AMOUNT = 999

print("=" * 50)
print("  Hyperliquid Testnet - Spot to Perp Transfer")
print("=" * 50)
print(f"  Wallet: {WALLET_ADDRESS}")
print(f"  Amount: {AMOUNT} USDC")
print()

# Check spot balance first
url = "https://api.hyperliquid-testnet.xyz/info"
data = {"type": "spotClearinghouseState", "user": WALLET_ADDRESS}
r = requests.post(url, json=data, timeout=15)
spot = r.json()
for b in spot.get("balances", []):
    if b["coin"] == "USDC" and float(b["total"]) > 0:
        print(f"  Spot USDC: {b['total']}")
        break

# Load markets from MAINNET (works), use for transfer on testnet
print("  Loading mainnet markets (for market data)...")
sync_main = ccxt.hyperliquid({"options": {"defaultType": "swap"}})
sync_main.load_markets()
print(f"  OK: {len(sync_main.markets)} markets")

# Create testnet exchange with injected markets
ex = ccxt.hyperliquid({
    "privateKey": PRIVATE_KEY,
    "walletAddress": WALLET_ADDRESS,
    "options": {"defaultType": "swap"},
    "sandbox": True,
})
ex.markets = sync_main.markets
ex.markets_by_id = sync_main.markets_by_id
ex.symbols = sync_main.symbols
ex.ids = sync_main.ids
ex.currencies = sync_main.currencies
ex.currencies_by_id = sync_main.currencies_by_id

print(f"  Transferring {AMOUNT} USDC: Spot -> Perp...")
try:
    result = ex.transfer("USDC", AMOUNT, "spot", "swap")
    print(f"  OK: {result}")
except Exception as e:
    print(f"  Transfer error: {type(e).__name__}: {e}")
    print("  Trying alternative method...")
    try:
        # Try using usdClassTransfer directly
        result = ex.private_post_exchange({
            "action": {
                "type": "usdClassTransfer",
                "hyperliquidChain": "Testnet",
                "signatureChainId": "0x66eee",
                "amount": str(AMOUNT),
                "toPerp": True,
            },
            "nonce": int(requests.post(url, json={"type": "clearinghouseState", "user": WALLET_ADDRESS}, timeout=15).json().get("time", 0)),
        })
        print(f"  OK: {result}")
    except Exception as e2:
        print(f"  Alt error: {type(e2).__name__}: {e2}")

# Verify perp balance
data = {"type": "clearinghouseState", "user": WALLET_ADDRESS}
r = requests.post(url, json=data, timeout=15)
balance = r.json()
acct = balance["marginSummary"]["accountValue"]
print(f"\n  Perp balance: ${acct}")
print("=" * 50)
