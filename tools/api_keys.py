"""API key management tools."""

import json
import hashlib
import secrets
import time
from auth import _extract_caller_email
import db as _db


def _sha256_hex(value: str) -> str:
    """Hex-encode SHA-256 hash of value."""
    return hashlib.sha256(value.encode()).hexdigest()


def generate_api_key(label: str, ctx=None) -> str:
    """Generate an A2A API key for the authenticated caller.

    The key is returned once. Store it securely — it cannot be recovered later.
    Returns JSON with: api_key, hash, label, created_at
    """
    caller = _extract_caller_email(ctx)
    if not caller:
        return json.dumps({"error": "Not authenticated"})

    api_key = "sk_" + secrets.token_urlsafe(48)
    key_hash = _sha256_hex(api_key)

    try:
        _db.store_api_key(key_hash, label, caller)
        return json.dumps({
            "api_key": api_key,
            "hash": key_hash,
            "label": label,
            "created_at": time.time(),
            "hint": "Store api_key securely — it cannot be recovered"
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


def list_api_keys(ctx=None) -> str:
    """List all API keys for the authenticated caller."""
    caller = _extract_caller_email(ctx)
    if not caller:
        return json.dumps({"error": "Not authenticated"})

    try:
        keys = _db.list_api_keys(caller)
        return json.dumps(keys, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


def revoke_api_key(label: str, ctx=None) -> str:
    """Revoke an API key by label (caller can only revoke own keys)."""
    caller = _extract_caller_email(ctx)
    if not caller:
        return json.dumps({"error": "Not authenticated"})

    try:
        result = _db.revoke_api_key(label, caller)
        return json.dumps({"revoked": label, "status": result})
    except Exception as e:
        return json.dumps({"error": str(e)})


def register(mcp):
    """Register API key tools with mcp instance."""
    mcp.tool()(generate_api_key)
    mcp.tool()(list_api_keys)
    mcp.tool()(revoke_api_key)
