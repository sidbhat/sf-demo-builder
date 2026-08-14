#!/usr/bin/env python3
"""
SF Demo Builder MCP Server
Two-phase: design_demo_org → provision_demo_org

Phase 1: design_demo_org
  Takes customer + industry + country + business_problem + n_employees.
  Returns a complete org plan: roles, scenario narrative, Joule prompts,
  and an honest capability manifest (what will be live vs story).

Phase 2: provision_demo_org
  Takes the org plan from Phase 1 and provisions everything it can actually
  deliver via SF OData. Reports clearly on what was created vs what remains
  as narrative.

Auth: XSUAA / SAP IDP principal propagation.
  Run with --http for HTTP+SSE transport (required for OAuth2).
  Bearer JWT is validated against accounts.sap.com JWKS.
  The caller's SAP email is extracted and the +alias suffix is used
  to default the demo persona (e.g. +se.ceo -> SF user se.ceo).
"""

try:
    from fastmcp import FastMCP
except ImportError:
    from mcp.server.fastmcp import FastMCP

import json
import os
import sys
import contextvars
from typing import Optional

import db as _db

# ── FastMCP initialization ────────────────────────────────────────────────────

PORT = int(os.environ.get("PORT", "8000"))
_HTTP_MODE = "--http" in sys.argv or "VCAP_APPLICATION" in os.environ

XSUAA_JWKS_URI = os.environ.get("XSUAA_JWKS_URI", "https://accounts.sap.com/oauth2/certs")
XSUAA_ISSUER = os.environ.get("XSUAA_ISSUER", "https://accounts.sap.com")
SERVER_BASE_URL = os.environ.get("SERVER_BASE_URL", "http://localhost:8000")

# ContextVar for API key auth
_api_key_caller: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "api_key_caller", default=None
)
_is_a2a_request: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "is_a2a_request", default=False
)


def _vcap_xsuaa_creds() -> Optional[dict]:
    """Extract XSUAA credentials from VCAP_SERVICES (CF runtime)."""
    vcap = os.environ.get("VCAP_SERVICES")
    if not vcap:
        return None
    try:
        services = json.loads(vcap)
        instances = services.get("xsuaa", [])
        for inst in instances:
            if "sf-demo-builder" in inst.get("name", ""):
                return inst["credentials"]
        return instances[0]["credentials"] if instances else None
    except Exception:
        return None


def _build_auth():
    """Build auth provider for HTTP mode, None for stdio mode."""
    if not _HTTP_MODE:
        return None
    if os.environ.get("SKIP_MCP_AUTH", "").lower() in ("1", "true", "yes"):
        return None
    from fastmcp.server.auth import RemoteAuthProvider, JWTVerifier

    vcap_creds = _vcap_xsuaa_creds()
    if vcap_creds and vcap_creds.get("verificationkey"):
        base_url = vcap_creds.get("url", XSUAA_ISSUER).rstrip("/")
        issuer = base_url + "/oauth/token"
        verifier = JWTVerifier(
            public_key=vcap_creds["verificationkey"],
            issuer=issuer,
            algorithm="RS256",
        )
        auth_servers = [base_url + "/"]
    else:
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


mcp = FastMCP(
    name="sf-demo-builder",
    auth=_build_auth(),
)

# A2A instance has no auth — ApiKeyMiddleware enforces identity before it reaches here
mcp_a2a = FastMCP(
    name="sf-demo-builder",
    auth=None,
)

# ── Import and register all tool modules ──────────────────────────────────────

from tools import design, provision, query, config, content, api_keys

# Register all tools on both instances
for _m in (mcp, mcp_a2a):
    api_keys.register(_m)
    design.register(_m)
    provision.register(_m)
    query.register(_m)
    config.register(_m)
    content.register(_m)

# ── HTTP server setup (only if running with --http) ──────────────────────────

if __name__ == "__main__":
    if _HTTP_MODE:
        import hashlib
        import json
        from auth import LOGIN_URL
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import JSONResponse, Response
        from starlette.routing import Route, Mount
        from starlette.types import ASGIApp, Receive, Scope, Send
        import uvicorn

        port_arg = PORT
        for i, arg in enumerate(sys.argv):
            if arg == "--port" and i + 1 < len(sys.argv):
                port_arg = int(sys.argv[i + 1])
        print(f"Starting SF Demo Builder MCP (HTTP mode) on port {port_arg}")
        print(f"  Auth XSUAA: {'VCAP verificationkey' if _vcap_xsuaa_creds() else XSUAA_JWKS_URI}")
        print(f"  Auth A2A  : X-API-Key header at /a2a/mcp")
        print(f"  Base URL  : {SERVER_BASE_URL}")

        def _sha256_hex(value: str) -> str:
            """Hex-encode SHA-256 hash of value."""
            return hashlib.sha256(value.encode()).hexdigest()

        class ApiKeyMiddleware:
            """Validate X-API-Key for /a2a/* requests."""
            def __init__(self, app: ASGIApp) -> None:
                self.app = app

            async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
                if scope["type"] != "http":
                    await self.app(scope, receive, send)
                    return

                headers = dict(scope.get("headers", []))
                raw_key = (
                    headers.get(b"x-api-key", b"").decode()
                    or headers.get(b"X-API-Key", b"").decode()
                )

                if not raw_key:
                    await _send_401(send, "Missing X-API-Key header")
                    return

                key_hash = _sha256_hex(raw_key)
                owner = _db.lookup_api_key(key_hash)
                if not owner:
                    await _send_401(send, "Invalid or revoked API key")
                    return

                tok1 = _api_key_caller.set(owner)
                tok2 = _is_a2a_request.set(True)
                try:
                    _db.touch_api_key(key_hash)
                    await self.app(scope, receive, send)
                finally:
                    _api_key_caller.reset(tok1)
                    _is_a2a_request.reset(tok2)

        async def _send_401(send: Send, detail: str) -> None:
            body = json.dumps({"error": detail, "hint": "Provide a valid X-API-Key header"}).encode()
            await send({"type": "http.response.start", "status": 401,
                        "headers": [[b"content-type", b"application/json"],
                                    [b"content-length", str(len(body)).encode()]]})
            await send({"type": "http.response.body", "body": body})

        async def health(request: Request):
            return JSONResponse({"status": "ok", "service": "sf-demo-builder"})

        async def info(request: Request):
            base = SERVER_BASE_URL.rstrip("/")
            xsuaa_creds = _vcap_xsuaa_creds() or {}
            xsuaa_clientid = xsuaa_creds.get("clientid", "(see cf env sf-demo-builder → xsuaa credentials)")
            xsuaa_clientsecret = xsuaa_creds.get("clientsecret", "(see cf env sf-demo-builder → xsuaa credentials)")
            xsuaa_token_url = (xsuaa_creds.get("url") or "https://six-ai.authentication.us10.hana.ondemand.com") + "/oauth/token"
            xsuaa_auth_url = (xsuaa_creds.get("url") or "https://six-ai.authentication.us10.hana.ondemand.com") + "/oauth/authorize"
            tools = [
                {"name": "design_demo_org", "desc": "Design a demo org plan (personas, goals, scenario)"},
                {"name": "provision_demo_org", "desc": "Start background provisioning — returns job_id immediately"},
                {"name": "get_provisioning_status", "desc": "Poll a provisioning job by job_id (returns credentials when done)"},
                {"name": "list_my_orgs", "desc": "List all demo orgs you have provisioned"},
                {"name": "get_org_details", "desc": "Full metadata for one org by demo_id"},
                {"name": "get_org_employees", "desc": "All employees + credentials for a demo org"},
                {"name": "get_org_goals", "desc": "Goal assignments per employee for a demo org"},
                {"name": "get_org_compensation", "desc": "Salary history + bonus data for a demo org"},
                {"name": "get_org_talent", "desc": "Talent profiles (impact/risk/future leader)"},
                {"name": "delete_demo_org", "desc": "Permanently delete a demo org from SF and HANA (confirmation required)"},
                {"name": "generate_api_key", "desc": "Generate an A2A API key (JWT path only)"},
                {"name": "list_api_keys", "desc": "List your API keys (label, last used, active)"},
                {"name": "revoke_api_key", "desc": "Revoke an API key by label"},
                {"name": "configure_sf_instance", "desc": "Save a custom SF instance config (API URL, admin creds, IAS)"},
                {"name": "get_sf_instance_config", "desc": "Show your current SF instance config"},
                {"name": "reset_sf_instance_config", "desc": "Delete your custom SF config (revert to default instance)"},
                {"name": "list_scenarios", "desc": "List all available demo scenarios"},
                {"name": "generate_demo_script", "desc": "Generate two-surface demo script from a plan"},
                {"name": "generate_agent_card", "desc": "Generate Joule Agent Hub card from a plan"},
                {"name": "whoami", "desc": "Show your authenticated identity and SF instance config"},
            ]
            rows = "".join(f"<tr><td><code>{t['name']}</code></td><td>{t['desc']}</td></tr>" for t in tools)
            html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>SF Demo Builder — MCP Info</title></head><body>
<h1>SF Demo Builder</h1><p>Demo environment builder for SAP SuccessFactors.</p>
<h2>Endpoints</h2><ul>
<li><code>{base}/mcp</code> — OAuth2 (XSUAA Bearer JWT)</li>
<li><code>{base}/a2a/mcp</code> — X-API-Key header</li>
<li><code>{base}/health</code> — CF health check</li>
<li><code>{base}/info</code> — This page</li>
</ul>
<h2>Available tools ({len(tools)})</h2><table border="1" cellpadding="8"><tr><th>Tool</th><th>Description</th></tr>{rows}</table>
</body></html>"""
            return Response(html, media_type="text/html")

        if hasattr(mcp, "streamable_http_app"):
            mcp_asgi     = mcp.streamable_http_app(path="/", stateless_http=True)
            mcp_a2a_asgi = mcp_a2a.streamable_http_app(path="/", stateless_http=True)
        else:
            mcp_asgi     = mcp.http_app(path="/", stateless_http=True)
            mcp_a2a_asgi = mcp_a2a.http_app(path="/", stateless_http=True)

        a2a_asgi = ApiKeyMiddleware(mcp_a2a_asgi)

        def _make_prefix_stripper(prefix: str, inner: ASGIApp):
            async def _app(scope, receive, send):
                if scope["type"] == "http":
                    path = scope.get("path", "")
                    if path.startswith(prefix):
                        scope = dict(scope)
                        scope["path"] = path[len(prefix):] or "/"
                        scope["raw_path"] = scope["path"].encode()
                return await inner(scope, receive, send)
            return _app

        mcp_stripped = _make_prefix_stripper("/mcp", mcp_asgi)
        a2a_stripped = _make_prefix_stripper("/a2a/mcp", a2a_asgi)

        from starlette.routing import Router as _Router
        from starlette.routing import Route as _Route
        _static_app = _Router(routes=[
            _Route("/health", health),
            _Route("/info", info),
        ])

        async def dispatch(scope, receive, send):
            path = scope.get("path", "")
            if path.startswith("/health"):
                await _static_app(scope, receive, send)
            elif path.startswith("/info"):
                await _static_app(scope, receive, send)
            elif path.startswith("/.well-known/"):
                await mcp_asgi(scope, receive, send)
            elif path.startswith("/a2a/mcp"):
                await a2a_stripped(scope, receive, send)
            elif path.startswith("/mcp"):
                await mcp_stripped(scope, receive, send)
            else:
                body = b"Not Found"
                await send({"type": "http.response.start", "status": 404,
                            "headers": [[b"content-length", str(len(body)).encode()]]})
                await send({"type": "http.response.body", "body": body})

        # Helper: wrap an ASGI lifespan as an async context manager
        import contextlib
        import anyio

        @contextlib.asynccontextmanager
        async def _start_mcp(asgi_app):
            """Drive an MCP ASGI app's lifespan startup/shutdown in a background task."""
            startup_complete = anyio.Event()
            shutdown_event   = anyio.Event()

            async def _lifespan():
                msg_queue = []
                async def _recv():
                    if not msg_queue:
                        await startup_complete.wait()
                        await shutdown_event.wait()
                        return {"type": "lifespan.shutdown"}
                    return msg_queue.pop(0)
                msg_queue.append({"type": "lifespan.startup"})
                async def _send(msg):
                    if msg["type"] == "lifespan.startup.complete":
                        startup_complete.set()
                await asgi_app({"type": "lifespan", "asgi": {"version": "3.0"}}, _recv, _send)

            async with anyio.create_task_group() as tg:
                tg.start_soon(_lifespan)
                await startup_complete.wait()
                try:
                    yield
                finally:
                    shutdown_event.set()

        class _AppWithLifespan:
            async def __call__(self, scope, receive, send):
                if scope["type"] == "lifespan":
                    async with _start_mcp(mcp_asgi), _start_mcp(mcp_a2a_asgi):
                        await send({"type": "lifespan.startup.complete"})
                        msg = await receive()
                        assert msg["type"] == "lifespan.shutdown"
                        await send({"type": "lifespan.shutdown.complete"})
                else:
                    await dispatch(scope, receive, send)

        app = _AppWithLifespan()
        uvicorn.run(app, host="0.0.0.0", port=port_arg)
    else:
        mcp.run()
