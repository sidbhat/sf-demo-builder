"""
SF OData API client functions.
"""

import json
import base64
import urllib.request
import urllib.parse
from typing import Optional

from auth import CTX

# Re-export for dependencies
from auth import SF_CREDS


def _sf_upsert(rows: list, sf: dict) -> tuple[int, list]:
    """Returns (ok_count, error_messages)."""
    if not rows:
        return 0, []
    body = json.dumps(rows).encode()
    req = urllib.request.Request(f"{sf['sf_base']}/upsert", data=body, headers=sf['sf_headers'], method="POST")
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=60) as r:
            resp = json.loads(r.read())
            results = resp.get("d", [])
            ok = 0
            errors = []
            for item in results:
                s = item.get("status", "")
                m = (item.get("message") or "")
                if s == "OK" or "already exists" in m.lower() or "no new changes" in m.lower():
                    ok += 1
                else:
                    errors.append(f"{item.get('key','?')}: {m[:120]}")
            return ok, errors
    except urllib.error.HTTPError as e:
        err = e.read().decode(errors="replace")[:200]
        return 0, [f"HTTP {e.code}: {err}"]


def _sf_delete(uri: str, sf: dict) -> tuple[bool, str]:
    """DELETE a single SF entity by its OData URI path (e.g. "User('NK001')")."""
    req = urllib.request.Request(
        f"{sf['sf_base']}/{uri}",
        headers=sf['sf_headers'], method="DELETE")
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=30):
            return True, "ok"
    except urllib.error.HTTPError as e:
        err = e.read().decode(errors="replace")
        try:
            msg = json.loads(err)["error"]["message"]["value"]
        except Exception:
            msg = err[:200]
        if e.code == 404:
            return True, "not_found"
        return False, msg[:120]
    except Exception as e:
        return False, str(e)[:120]


def _sf_post(entity: str, row: dict, sf: dict) -> tuple[bool, str]:
    """POST to SF OData."""
    body = json.dumps(row).encode()
    req = urllib.request.Request(f"{sf['sf_base']}/{entity}", data=body, headers=sf['sf_headers'], method="POST")
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=30):
            return True, "ok"
    except urllib.error.HTTPError as e:
        err = e.read().decode(errors="replace")
        try:
            msg = json.loads(err)["error"]["message"]["value"]
        except Exception:
            msg = err[:200]
        if "already exists" in msg.lower() or "duplicate" in msg.lower():
            return True, "already_exists"
        return False, msg[:120]


def _sf_post_as_user(entity: str, row: dict, username: str, password: str, sf: dict) -> tuple[bool, str]:
    """POST to SF OData for Goal entities.
    Try as the user first; fall back to sfadmin if user lacks OData permission (LGN0002)."""
    full_user = f"{username}@{sf['company_code']}" if "@" not in username else username
    creds = base64.b64encode(f"{full_user}:{password}".encode()).decode()
    hdrs = {**sf['sf_headers'], "Authorization": f"Basic {creds}"}
    body = json.dumps(row).encode()
    req = urllib.request.Request(
        f"{sf['sf_base']}/{entity}",
        data=body, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=30):
            return True, "ok"
    except urllib.error.HTTPError as e:
        err = e.read().decode(errors="replace")
        try:
            msg = json.loads(err)["error"]["message"]["value"]
        except Exception:
            msg = err[:200]
        # LGN0002 = user lacks OData API permission — fall back to sfadmin
        if "lgn0002" in msg.lower() or "admin permission" in msg.lower():
            return _sf_post(entity, row, sf)
        if "already exists" in msg.lower() or "duplicate" in msg.lower():
            return True, "already_exists"
        return False, msg[:120]
