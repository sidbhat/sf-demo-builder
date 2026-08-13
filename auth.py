"""
XSUAA + API key authentication and SF config builder.
"""

import json
import base64
import os
import ssl
import urllib.request
import urllib.parse
from typing import Optional

import db as _db

# ── Auth configuration ────────────────────────────────────────────────────────
# SAP accounts.sap.com OIDC — tokens issued by SAP employee login
XSUAA_JWKS_URI = os.environ.get(
    "XSUAA_JWKS_URI", "https://accounts.sap.com/oauth2/certs"
)
XSUAA_ISSUER = os.environ.get(
    "XSUAA_ISSUER", "https://accounts.sap.com"
)
# Public base URL for this server (used in Protected Resource metadata)
SERVER_BASE_URL = os.environ.get(
    "SERVER_BASE_URL", "http://localhost:8000"
)

# Email prefix for +alias SF users (overridable so different deployers can use their own)
DEFAULT_EMAIL_PREFIX = os.environ.get("EMAIL_PREFIX", "siddhartha.bhattacharya")

# ── SSL context for HTTPS calls ───────────────────────────────────────────────
CTX = ssl.create_default_context()
# Load system CA bundle — works on macOS and Linux
for _ca in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"):
    if os.path.exists(_ca):
        CTX.load_verify_locations(_ca)
        break

# ── SF / IAS credentials (from env, with demo fallbacks) ──────────────────────
SF_BASE     = "https://apisalesdemo8.successfactors.com/odata/v2"
_sf_user    = os.environ.get("SF_ADMIN_USER", "sfadmin@SFSALES011375")
_sf_pass    = os.environ.get("SF_ADMIN_PASS", "DemoHCM25!")
SF_CREDS    = base64.b64encode(f"{_sf_user}:{_sf_pass}".encode()).decode()

IAS_BASE          = "https://abncw6hc7.accounts.cloud.sap"
IAS_CLIENT_ID     = os.environ.get("IAS_CLIENT_ID",     "973ed475-454d-4069-8240-46cdb757e799")
IAS_CLIENT_SECRET = os.environ.get("IAS_CLIENT_SECRET", "/.hMd?P7RjD:[S_a-CX[oAWOlUEUPYRVdk")

LOGIN_URL = "https://hcm-us10-sales.hr.cloud.sap/login?company=SFSALES011375"


def _vcap_xsuaa_creds() -> Optional[dict]:
    """Extract XSUAA credentials from VCAP_SERVICES (CF runtime)."""
    vcap = os.environ.get("VCAP_SERVICES")
    if not vcap:
        return None
    try:
        services = json.loads(vcap)
        instances = services.get("xsuaa", [])
        # prefer the one named for this app
        for inst in instances:
            if "sf-demo-builder" in inst.get("name", ""):
                return inst["credentials"]
        return instances[0]["credentials"] if instances else None
    except Exception:
        return None


def _build_auth():
    """Build auth provider for HTTP mode. Returns None for stdio mode."""
    from fastmcp.server.fastmcp import FastMCP

    # Check if we're in HTTP mode
    import sys
    _HTTP_MODE = "--http" in sys.argv or "VCAP_APPLICATION" in os.environ

    if not _HTTP_MODE:
        return None
    if os.environ.get("SKIP_MCP_AUTH", "").lower() in ("1", "true", "yes"):
        return None
    from fastmcp.server.auth import RemoteAuthProvider, JWTVerifier

    vcap_creds = _vcap_xsuaa_creds()
    if vcap_creds and vcap_creds.get("verificationkey"):
        # CF runtime: use inline RSA public key from XSUAA service binding.
        # XSUAA tokens have iss = <url>/oauth/token — must match exactly.
        base_url = vcap_creds.get("url", XSUAA_ISSUER).rstrip("/")
        issuer    = base_url + "/oauth/token"
        verifier  = JWTVerifier(
            public_key=vcap_creds["verificationkey"],
            issuer=issuer,
            algorithm="RS256",
            # No audience check — XSUAA tokens carry ['uaa', clientid] in aud;
            # issuer validation is sufficient to establish token provenance.
        )
        # Tell Joule/MCP clients where to get tokens (the base UAA URL, not /oauth/token)
        auth_servers = [base_url + "/"]
    else:
        # Local / custom: use JWKS URI
        verifier = JWTVerifier(
            jwks_uri=XSUAA_JWKS_URI,
            issuer=XSUAA_ISSUER,
            algorithm="RS256",
        )
        auth_servers = [XSUAA_ISSUER]

    return RemoteAuthProvider(
        token_verifier=verifier,
        authorization_servers=auth_servers,
        base_url=SERVER_BASE_URL,
        resource_name="SF Demo Builder",
    )


def _detect_ias_from_login_url(login_url: str) -> Optional[str]:
    """Follow the SF login URL redirect chain to discover the IAS tenant base URL.

    SF login → /saml2/Login (internal) → IAS hostname (accounts.cloud.sap / accounts.ondemand.com).
    Follows up to 4 redirect hops so we reach the IAS URL even when SF issues
    an internal relative redirect first.
    Returns the IAS base URL (scheme+host) or None if no IAS redirect found.
    """
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):
            return None

    opener = urllib.request.build_opener(_NoRedirect())

    current_url = login_url
    for _ in range(4):
        try:
            req = urllib.request.Request(
                current_url,
                headers={"User-Agent": "Mozilla/5.0"},
                method="GET",
            )
            try:
                opener.open(req, timeout=10)
                break  # 200 — end of chain, no IAS redirect found
            except urllib.error.HTTPError as e:
                loc = e.headers.get("Location", "")
                if not loc:
                    break
                # Resolve relative redirects against current URL
                loc = urllib.parse.urljoin(current_url, loc)
                print(f"[ias_detect] hop: {loc[:100]}", flush=True)
                parsed = urllib.parse.urlparse(loc)
                host = parsed.netloc.lower()
                if ".accounts.cloud.sap" in host or ".accounts.ondemand.com" in host:
                    return f"{parsed.scheme}://{parsed.netloc}"
                current_url = loc
        except Exception as e:
            print(f"[ias_detect] error: {e}", flush=True)
            break
    return None


def _build_sf_config(caller_email: Optional[str]) -> dict:
    """Return the SF + IAS config dict for this caller.

    Checks DEMO_SF_CONFIGS for a custom config; falls back to env defaults.
    Returns:
      sf_base       - OData API base URL (no trailing slash)
      sf_headers    - dict with Authorization/Accept/Content-Type
      admin_user    - full username incl company code (e.g. sfadmin@SFSALES011375)
      company_code  - extracted company code (e.g. SFSALES011375)
      login_url     - browser login URL
      ias_base      - IAS tenant base URL or None
      ias_scim_url  - IAS SCIM Users URL or None
      ias_auth      - Basic auth header value for IAS or None
    """
    custom = _db.get_sf_config(caller_email) if caller_email else None

    if custom:
        api_base   = custom["api_base_url"].rstrip("/")
        admin_user = custom["admin_user"]
        admin_pass = custom["admin_pass"]
        login_url  = custom["login_url"]
        ias_base   = custom.get("ias_base_url")
        ias_cid    = custom.get("ias_client_id")
        ias_csec   = custom.get("ias_client_secret")
    else:
        api_base   = SF_BASE.rstrip("/")
        admin_user = _sf_user
        admin_pass = _sf_pass
        login_url  = LOGIN_URL
        ias_base   = IAS_BASE
        ias_cid    = IAS_CLIENT_ID
        ias_csec   = IAS_CLIENT_SECRET

    creds = base64.b64encode(f"{admin_user}:{admin_pass}".encode()).decode()
    headers = {
        "Authorization": f"Basic {creds}",
        "Accept":        "application/json",
        "Content-Type":  "application/json",
    }
    # Company code is the part after @ in admin_user (e.g. sfadmin@SFSALES011375)
    company_code = admin_user.split("@", 1)[1] if "@" in admin_user else "UNKNOWN"

    ias_scim = None
    ias_auth_hdr = None
    if ias_base and ias_cid and ias_csec:
        ias_scim = ias_base.rstrip("/") + "/service/scim/Users"
        ias_auth_hdr = "Basic " + base64.b64encode(f"{ias_cid}:{ias_csec}".encode()).decode()

    return {
        "sf_base":      api_base,
        "sf_headers":   headers,
        "admin_user":   admin_user,
        "admin_pass":   admin_pass,
        "company_code": company_code,
        "login_url":    login_url,
        "ias_base":     ias_base if (ias_scim) else None,
        "ias_scim_url": ias_scim,
        "ias_auth":     ias_auth_hdr,
    }


def _extract_caller_email(ctx=None) -> Optional[str]:
    """Return the authenticated caller's email.

    Priority:
    1. API-key path — ContextVar set by ApiKeyMiddleware before forwarding the request
    2. JWT path — FastMCP get_access_token() reads AccessToken.claims from HTTP scope
    3. None in stdio mode (no auth present)
    """
    import contextvars
    import sys

    # ContextVars defined in server.py
    # We'll handle these via dependency injection
    _HTTP_MODE = "--http" in sys.argv or "VCAP_APPLICATION" in os.environ

    # A2A / API-key path — ContextVar set by ApiKeyMiddleware
    # Note: These are set in server.py ApiKeyMiddleware
    try:
        from server import _api_key_caller
        api_key_email = _api_key_caller.get()
        if api_key_email:
            return api_key_email
    except Exception:
        pass

    if not _HTTP_MODE:
        return None
    try:
        from fastmcp.server.dependencies import get_access_token
        token = get_access_token()
        if token is not None:
            claims: dict = token.claims or {}
            print(f"[auth] get_access_token claims keys={list(claims.keys())} "
                  f"email={claims.get('email')!r} user_name={claims.get('user_name')!r} "
                  f"sub={claims.get('sub')!r} client_id={token.client_id!r}", flush=True)
            # XSUAA tokens carry email in "email", "user_name", or "sub"
            email = (claims.get("email")
                     or claims.get("user_name")
                     or claims.get("sub"))
            if email and "@" in str(email):
                return str(email)
            # client_id itself won't have @, but log it for debugging
    except Exception as exc:
        print(f"[auth] _extract_caller_email error: {exc}", flush=True)
    return None


def _alias_from_email(email: Optional[str]) -> Optional[str]:
    """Parse +alias from SAP email for principal propagation.

    siddhartha.bhattacharya+se.ceo@sap.com  ->  se.ceo
    user@sap.com                             ->  None (no alias)
    """
    if not email or "+" not in email:
        return None
    local = email.split("@")[0]          # siddhartha.bhattacharya+se.ceo
    plus_idx = local.index("+")
    return local[plus_idx + 1:]          # se.ceo
