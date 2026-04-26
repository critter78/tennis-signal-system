"""
One-time script to generate Polymarket CLOB API credentials.
Run this locally, then paste the output into your .env file.

Usage:
  pip install py-clob-client python-dotenv
  python generate_api_keys.py
"""

from py_clob_client.client import ClobClient
from dotenv import load_dotenv
from eth_account import Account
import os

load_dotenv()
pk = os.getenv("POLYMARKET_PRIVATE_KEY")

if not pk:
    print("ERROR: Set POLYMARKET_PRIVATE_KEY in your .env file first")
    exit(1)

# Derive the EOA address from the private key
eoa = Account.from_key(pk)
print(f"EOA (signer) address: {eoa.address}")

# Your Polymarket proxy wallet address
PROXY_ADDRESS = "0x2073E042657364539e4e44Fb991fe5Ea6D7271E7"
print(f"Proxy wallet address: {PROXY_ADDRESS}")

# Try each signature type with both create and derive methods
for sig_type, label in [(2, "POLY_PROXY"), (1, "POLY_GNOSIS_SAFE"), (0, "EOA")]:
    if sig_type == 0:
        client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=pk,
        )
    else:
        client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=pk,
            signature_type=sig_type,
            funder=PROXY_ADDRESS,
        )

    # Try derive first (for existing accounts), then create
    for method_name in ["derive_api_key", "create_or_derive_api_key", "create_api_key"]:
        method = getattr(client, method_name, None)
        if not method:
            continue
        try:
            print(f"Trying {method_name}() with {label}...")
            creds = method()
            print(f"\nSUCCESS with {method_name}() + {label}!\n")
            # Handle both dict and object responses
            if isinstance(creds, dict):
                ak, sec, pp = creds['apiKey'], creds['secret'], creds['passphrase']
            else:
                ak = getattr(creds, 'api_key', None) or getattr(creds, 'apiKey', '')
                sec = getattr(creds, 'secret', '') or getattr(creds, 'api_secret', '')
                pp = getattr(creds, 'passphrase', '') or getattr(creds, 'api_passphrase', '')
            print(f"Paste these into your .env file:\n")
            print(f"POLYMARKET_API_KEY={ak}")
            print(f"POLYMARKET_API_SECRET={sec}")
            print(f"POLYMARKET_API_PASSPHRASE={pp}")
            print(f"POLYMARKET_SIGNATURE_TYPE={sig_type}")
            print(f"POLYMARKET_PROXY_ADDRESS={PROXY_ADDRESS}")
            exit(0)
        except Exception as e:
            print(f"  Failed: {e}")

print("\nAll methods failed.")
print("You may need to enable CLOB trading first.")
print("Try: go to polymarket.com, place a small manual trade, then re-run this script.")
