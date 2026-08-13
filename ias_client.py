"""
IAS (Identity Authentication Service) SCIM client functions.
"""

import json
import urllib.request
import urllib.parse
import secrets
from typing import Optional

from auth import CTX, _build_sf_config


def _ias_get_user(username: str, sf: dict):
    """Get a user from IAS SCIM endpoint."""
    ias_scim = sf.get("ias_scim_url")
    ias_auth = sf.get("ias_auth")
    if not ias_scim or not ias_auth:
        return None
    url = ias_scim + "?filter=" + urllib.parse.quote(f'userName eq "{username}"')
    req = urllib.request.Request(url, headers={"Authorization": ias_auth, "Accept": "application/scim+json"})
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=15) as r:
            d = json.loads(r.read())
            return (d.get("Resources") or [None])[0]
    except Exception:
        return None


def _ias_ensure_user(username: str, password: str, email: str,
                     first_name: str = "", last_name: str = "",
                     sf: Optional[dict] = None) -> tuple[bool, str]:
    """Create or update an IAS user. Skipped if IAS not configured in sf config."""
    if sf is None:
        sf = _build_sf_config(None)
    ias_scim = sf.get("ias_scim_url")
    ias_auth = sf.get("ias_auth")
    if not ias_scim or not ias_auth:
        return True, "ias_skipped"

    existing = _ias_get_user(username, sf)
    if existing:
        scim_id = existing["id"]
    else:
        # Create the user
        payload = json.dumps({
            "userName": username,
            "name": {"givenName": first_name, "familyName": last_name},
            "emails": [{"value": email, "primary": True, "type": "work"}],
            "userType": "employee",
            "active": True,
        }).encode()
        req = urllib.request.Request(
            ias_scim, data=payload, method="POST",
            headers={"Authorization": ias_auth, "Content-Type": "application/scim+json",
                     "Accept": "application/scim+json"})
        try:
            with urllib.request.urlopen(req, context=CTX, timeout=15) as r:
                created = json.loads(r.read())
                scim_id = created.get("id", "")
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if "already exists" in body.lower() or e.code == 409:
                # Race condition — fetch and continue
                existing = _ias_get_user(username, sf)
                if existing:
                    scim_id = existing["id"]
                else:
                    return False, f"Create failed and not found: HTTP {e.code}"
            else:
                return False, f"Create HTTP {e.code}: {body[:120]}"

    # Set password and email via PUT
    user_data = _ias_get_user(username, sf) or {}
    user_data["password"] = password
    user_data["emails"] = [{"value": email, "primary": True, "type": "work"}]
    for f in ["meta", "groups"]:
        user_data.pop(f, None)
    payload = json.dumps(user_data).encode()
    req = urllib.request.Request(
        f"{ias_scim}/{scim_id}", data=payload, method="PUT",
        headers={"Authorization": ias_auth, "Content-Type": "application/scim+json",
                 "Accept": "application/scim+json"})
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=15) as r:
            return True, str(r.status)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        # IAS password history policy — retry with a fresh random password
        if e.code == 400 and "password" in body.lower() and "history" in body.lower():
            user_data["password"] = "Demo" + secrets.token_hex(3).upper() + "26!"
            payload2 = json.dumps(user_data).encode()
            req2 = urllib.request.Request(
                f"{ias_scim}/{scim_id}", data=payload2, method="PUT",
                headers={"Authorization": ias_auth, "Content-Type": "application/scim+json",
                         "Accept": "application/scim+json"})
            try:
                with urllib.request.urlopen(req2, context=CTX, timeout=15) as r2:
                    return True, f"retry_ok:{r2.status}"
            except urllib.error.HTTPError as e2:
                return False, f"PUT retry HTTP {e2.code}: {e2.read().decode(errors='replace')[:80]}"
        return False, f"PUT HTTP {e.code}: {body[:100]}"


def _ias_set_password(username: str, password: str, email: str, sf: Optional[dict] = None) -> tuple[bool, str]:
    """Kept for backwards compat — delegates to _ias_ensure_user."""
    return _ias_ensure_user(username, password, email, sf=sf)
