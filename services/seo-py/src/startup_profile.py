"""Strict nonsecret startup selection; no admission or Ledger authorization."""
import os

def existing_store_profile():
    value = os.environ.get("ZBM_STARTUP_PROFILE", "normal")
    if value not in ("normal", "existing-store"):
        raise ValueError("invalid ZBM_STARTUP_PROFILE")
    return value == "existing-store"
