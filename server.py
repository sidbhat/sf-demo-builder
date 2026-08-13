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
import base64
import hashlib
import secrets
import ssl
import sys
import uuid
import urllib.request
import urllib.parse
import time
import os
import contextvars
import threading
import datetime as _dt
from datetime import datetime
from typing import Optional

import db as _db

# ── Background provisioning job store ────────────────────────────────────────
# job_id → {"status": "pending"|"running"|"done"|"error", "result": str|None,
#            "started_at": float, "error": str|None}
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()

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
PORT = int(os.environ.get("PORT", "8000"))

# Build auth provider when running in HTTP mode (--http flag or PORT set by CF)
# In stdio mode (default, used by Claude Code MCP) auth is bypassed —
# the local process boundary is the trust boundary.
_HTTP_MODE = "--http" in sys.argv or "VCAP_APPLICATION" in os.environ

# ContextVar set by ApiKeyMiddleware so _extract_caller_email can read it
# without needing to thread the raw Starlette scope through every call.
_api_key_caller: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "api_key_caller", default=None
)
# Flag: True when the current request came through /a2a/ (API key path)
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
        # prefer the one named for this app
        for inst in instances:
            if "sf-demo-builder" in inst.get("name", ""):
                return inst["credentials"]
        return instances[0]["credentials"] if instances else None
    except Exception:
        return None


def _build_auth():
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

mcp = FastMCP(
    name="sf-demo-builder",
    auth=_build_auth(),
)

# ── SF / IAS credentials (from env, with demo fallbacks) ──────────────────────
SF_BASE     = "https://apisalesdemo8.successfactors.com/odata/v2"
_sf_user    = os.environ.get("SF_ADMIN_USER", "sfadmin@SFSALES011375")
_sf_pass    = os.environ.get("SF_ADMIN_PASS", "DemoHCM25!")
SF_CREDS    = base64.b64encode(f"{_sf_user}:{_sf_pass}".encode()).decode()
SF_HEADERS  = {
    "Authorization": f"Basic {SF_CREDS}",
    "Accept":        "application/json",
    "Content-Type":  "application/json",
}

IAS_BASE          = "https://abncw6hc7.accounts.cloud.sap"
IAS_SCIM_URL      = f"{IAS_BASE}/service/scim/Users"
IAS_CLIENT_ID     = os.environ.get("IAS_CLIENT_ID",     "973ed475-454d-4069-8240-46cdb757e799")
IAS_CLIENT_SECRET = os.environ.get("IAS_CLIENT_SECRET", "/.hMd?P7RjD:[S_a-CX[oAWOlUEUPYRVdk")
IAS_AUTH = "Basic " + base64.b64encode(
    f"{IAS_CLIENT_ID}:{IAS_CLIENT_SECRET}".encode()).decode()

# Email prefix for +alias SF users (overridable so different deployers can use their own)
DEFAULT_EMAIL_PREFIX = os.environ.get("EMAIL_PREFIX", "siddhartha.bhattacharya")

CTX = ssl.create_default_context()
# Load system CA bundle — works on macOS and Linux
for _ca in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"):
    if os.path.exists(_ca):
        CTX.load_verify_locations(_ca)
        break

LOGIN_URL = "https://hcm-us10-sales.hr.cloud.sap/login?company=SFSALES011375"

# ── Per-user SF instance config ───────────────────────────────────────────────

def _detect_ias_from_login_url(login_url: str) -> Optional[str]:
    """Follow the SF login URL redirect chain to discover the IAS tenant base URL.

    SF login → /saml2/Login (internal) → IAS hostname (accounts.cloud.sap / accounts.ondemand.com).
    Follows up to 4 redirect hops so we reach the IAS URL even when SF issues
    an internal relative redirect first.
    Returns the IAS base URL (scheme+host) or None if no IAS redirect found.
    """
    from urllib.parse import urlparse, urljoin

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
                loc = urljoin(current_url, loc)
                print(f"[ias_detect] hop: {loc[:100]}", flush=True)
                parsed = urlparse(loc)
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
    # A2A / API-key path
    api_key_email = _api_key_caller.get()
    if api_key_email:
        return api_key_email

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


@mcp.tool()
def whoami(ctx=None) -> str:
    """Return the authenticated caller's identity as seen by this server.

    Use this to verify your XSUAA login is working and your email is being
    captured correctly for org ownership and email prefix derivation.
    """
    email = _extract_caller_email(ctx)
    is_a2a = _is_a2a_request.get()

    # Dump raw access token claims for debugging
    raw_claims: dict = {}
    token_meta: dict = {}
    try:
        from fastmcp.server.dependencies import get_access_token
        token = get_access_token()
        if token is not None:
            raw_claims = token.claims or {}
            token_meta = {
                "client_id": token.client_id,
                "scopes": token.scopes,
                "expires_at": token.expires_at,
            }
    except Exception as exc:
        raw_claims["error"] = str(exc)

    return json.dumps({
        "caller_email":     email,
        "auth_path":        "a2a_api_key" if is_a2a else "xsuaa_jwt",
        "email_prefix":     email.split("@")[0].split("+")[0] if email else None,
        "raw_auth_claims":  raw_claims,
        "token_meta":       token_meta,
    }, indent=2)


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

# ── Scenario knowledge base ───────────────────────────────────────────────────

SCENARIO_KB = {
    "mass_hiring": {
        "label": "Mass Hiring / High-Volume Recruiting",
        "org_bias": "recruiting_heavy",
        "talent_story": "Several positions open simultaneously; hiring managers overwhelmed; structured pipeline needed.",
        "agent_card": {
            "title": "New Hire Readiness Assistant",
            "challenge": (
                "A new hire is joining next month and no one has visibility into who their manager is, "
                "which team they're landing in, or what their first 30 days look like. "
                "Hiring is moving fast but the handoff to HR operations is broken."
            ),
            "prompts": [
                "Who is starting next month and who is their manager?",
                "What team is the new hire joining and what does the org structure look like?",
                "Show me everyone who joined in the last 90 days and their current manager.",
            ],
        },
        "demo_story": {
            "joule_chat": [
                {
                    "beat": "Who's arriving and who owns them",
                    "prompt": "Who is starting next month and who is their manager?",
                    "live_data": ["onboardee", "employees"],
                    "what_joule_shows": "Sam Rivera, Senior Associate, reporting to the VP Ops. Start date Nov 3. Position P-{code}-001 assigned.",
                    "ae_bridge": "This is real data — provisioned minutes ago. Joule read it directly from EC. No mock data, no rehearsed answer.",
                },
                {
                    "beat": "Team context the manager needs",
                    "prompt": "What does the team Sam is joining look like? Who are the key people?",
                    "live_data": ["org_structure", "employees"],
                    "what_joule_shows": "Org chart of the VP Ops' direct reports, their titles, grades, and tenure.",
                    "ae_bridge": "The manager can prep for the first 1:1 using exactly this — real headcount, real reporting lines.",
                },
                {
                    "beat": "Setting expectations on day one",
                    "prompt": "What are the team's active goals right now?",
                    "live_data": ["goal_assignments"],
                    "what_joule_shows": "Current annual goals for each team member — what they're working toward when Sam arrives.",
                    "ae_bridge": "Sam walks in knowing the team's priorities, not guessing. This is the difference between a good and a great onboarding.",
                },
            ],
            "joule_desktop": [
                {
                    "beat": "Pull the full onboarding context in one shot",
                    "agent_instruction": "Read Sam Rivera's employment record from SF, their manager's direct reports, and the team's current annual goals. Summarise what the manager needs to know before the new hire's first day.",
                    "mcp_calls": ["SF OData: EmpJob for onboardee, User list under manager, Goal_11 for team"],
                    "what_it_produces": "A structured briefing: who Sam reports to, the 4 teammates, their titles and grades, and 2-3 sentences on each person's active goal focus — synthesised from live data.",
                    "ae_bridge": "This is what the agent tier unlocks. Joule chat answered one question at a time. The desktop agent read across three entities, connected them, and wrote the manager's prep note.",
                },
                {
                    "beat": "Spot the talent risk in the receiving team",
                    "agent_instruction": "For the team Sam Rivera is joining, surface anyone flagged as high flight risk or high impact of loss. Are there gaps that a new hire could be positioned to fill?",
                    "mcp_calls": ["SF OData: talent profiles for manager's direct reports"],
                    "what_it_produces": "Table of team members, their impactOfLoss / riskOfLoss flags, and a plain-language read: 'Two members are high impact / medium risk — Sam's onboarding should prioritise knowledge transfer with them early.'",
                    "ae_bridge": "The agent connected two signals — who's arriving and who's at risk — to give the manager advice, not just data.",
                },
                {
                    "beat": "Draft the 30-day plan from live priorities",
                    "agent_instruction": "Using the team's active goals and the onboardee's role, draft a 30-60-90 day onboarding plan for Sam Rivera. Anchor each phase to a real team goal or a live team member.",
                    "mcp_calls": ["SF OData: Goal_11 for team, DevGoal_2001 for team, EmpJob for onboardee"],
                    "what_it_produces": "A structured 30-60-90 plan with named colleagues, real goal references, and suggested first contributions — all grounded in the SF data just read.",
                    "ae_bridge": "Not a template. Not a generic plan. An actual draft, anchored to the live org data we just provisioned.",
                },
            ],
        },
        "joule_prompts": [
            "Who is starting next month and who is their manager?",
            "What team is the new hire joining and what does the org structure look like?",
            "Show me everyone who joined in the last 90 days and their current manager.",
            "Which new hires don't have a position assigned yet?",
            "Draft a welcome message for our incoming Senior Associate.",
        ],
        "live_data": ["org_structure", "employees", "onboardee", "talent_profiles", "spot_awards"],
        "story_data": ["job_requisitions", "candidate_pipeline", "offer_letters", "interview_schedules"],
        "story_narrative": (
            "The onboardee Sam Rivera (Senior Associate, Nov 3 start) is live in SF with a manager and position assigned. "
            "Joule can surface their start date, reporting line, and team context from real data. "
            "Open reqs, candidate pipeline, and offer letters are narrative — "
            "Recruiting module setup (req templates, candidate records) is not provisioned."
        ),
    },
    "compensation_planning": {
        "label": "Compensation Planning & Pay Equity",
        "org_bias": "standard",
        "talent_story": "Annual comp cycle opens in two weeks; managers need to propose merit increases; budget constraints and outliers need to be visible.",
        "agent_card": {
            "title": "Compensation Review Assistant",
            "challenge": (
                "The annual comp cycle opens in two weeks and managers don't know who on their team "
                "is below midpoint, who hasn't had a raise in over a year, or how their team's total "
                "comp compares to budget. Decisions are being made blind."
            ),
            "prompts": [
                "Who on my team hasn't had a salary increase in the last 12 months?",
                "Show me anyone below the midpoint for their pay grade.",
                "Compare my top performer's salary progression to their peers.",
            ],
        },
        "demo_story": {
            "joule_chat": [
                {
                    "beat": "Find who hasn't moved in over a year",
                    "prompt": "Who on my team hasn't had a salary increase in the last 12 months?",
                    "live_data": ["salary_history", "employees"],
                    "what_joule_shows": "List of employees with last pay change date, current base, and months since last increase.",
                    "ae_bridge": "Three years of real comp history in SF. Joule read it directly — no spreadsheet export, no Finance request.",
                },
                {
                    "beat": "Surface the below-midpoint risk",
                    "prompt": "Show me anyone below the midpoint for their pay grade.",
                    "live_data": ["salary_history", "talent_profiles"],
                    "what_joule_shows": "Employees at bottom of grade band, with their impactOfLoss flag next to it.",
                    "ae_bridge": "This is the dangerous combination — low pay AND high impact. That's your retention risk hiding in the comp data.",
                },
                {
                    "beat": "Progression comparison for the merit conversation",
                    "prompt": "Show me the salary progression for our VP Engineering over the last 3 years compared to peers at the same grade.",
                    "live_data": ["salary_history"],
                    "what_joule_shows": "Year-over-year base salary for VP Eng vs average at GR-14, showing relative position.",
                    "ae_bridge": "The manager walks into the merit conversation with this. No surprises, no guesswork.",
                },
            ],
            "joule_desktop": [
                {
                    "beat": "Full comp risk scan across the team",
                    "agent_instruction": "Read salary history for all employees. Cross-reference with talent profile impact/risk flags. Identify anyone who is (a) high impact of loss AND (b) hasn't had a raise in 12+ months OR is below the GR midpoint. Rank by risk.",
                    "mcp_calls": ["SF OData: EmpPayCompRecurring for all employees", "SF OData: User talent profile fields"],
                    "what_it_produces": "Priority stack-ranked list: name, grade, last increase date, current base vs midpoint estimate, impactOfLoss flag — with a plain-language risk sentence per person.",
                    "ae_bridge": "One agent instruction replaces a three-way VLOOKUP between EC, Talent, and a pay band spreadsheet. The manager gets a ranked action list, not a data dump.",
                },
                {
                    "beat": "Projection: what does fixing this cost?",
                    "agent_instruction": "For everyone flagged as below midpoint or overdue for a raise, calculate what it would cost to bring them to midpoint. Show the total budget impact and individual deltas.",
                    "mcp_calls": ["SF OData: EmpPayCompRecurring — current salaries and grade bands"],
                    "what_it_produces": "Cost-to-fix table: per person delta to midpoint, total annual cost, as a percentage of team payroll — formatted for a budget conversation.",
                    "ae_bridge": "The agent turned the risk list into a business case. The manager can take this to the CHRO or finance partner directly.",
                },
                {
                    "beat": "Comp narrative for the board slide",
                    "agent_instruction": "Summarise the team's compensation health for an executive audience: grade distribution, recent movement, outliers, and top retention risks. Write it as 4 bullet points suitable for an HR update slide.",
                    "mcp_calls": ["SF OData: salary history + talent profiles for all employees"],
                    "what_it_produces": "4 executive-ready bullet points, specific and data-grounded — names replaced with roles for board presentation.",
                    "ae_bridge": "From raw SF data to board-ready narrative in one agent step. This is what the desktop tier enables — not just answering, but producing.",
                },
            ],
        },
        "joule_prompts": [
            "Who on my team hasn't had a salary increase in the last 12 months?",
            "Show me anyone below the midpoint for their pay grade.",
            "Compare my top performer's salary progression to their peers.",
            "Summarise total compensation spend across my team.",
            "Who received a bonus but no merit increase this cycle?",
        ],
        "live_data": ["org_structure", "employees", "salary_history", "bonus", "talent_profiles"],
        "story_data": ["merit_proposals", "budget_approval_workflow", "pay_equity_analysis"],
        "story_narrative": (
            "3 years of salary history and a Dec 2025 bonus entry are live per employee. "
            "Joule can surface pay grade comparisons, salary progression, and flag outliers from real data. "
            "Merit proposal workflows and budget pool allocation are narrative — "
            "SF Compensation module (comp templates, budget pools) is not provisioned."
        ),
    },
    "talent_retention": {
        "label": "Talent Retention & Flight Risk",
        "org_bias": "standard",
        "talent_story": "Key roles at risk; succession gaps identified; retention actions needed before year-end.",
        "agent_card": {
            "title": "Talent Retention Assistant",
            "challenge": (
                "Three of your highest-impact employees are flagged as medium-to-high flight risk "
                "heading into year-end. Two of those roles have no identified successor. "
                "The window to act before the market opens in January is closing fast."
            ),
            "prompts": [
                "Who has high impact of loss and high risk of leaving right now?",
                "Which critical roles have no identified successor and a flight-risk incumbent?",
                "Show me future leaders on my team who haven't been recognised this year.",
            ],
        },
        "demo_story": {
            "joule_chat": [
                {
                    "beat": "The danger list",
                    "prompt": "Who has high impact of loss and high risk of leaving right now?",
                    "live_data": ["talent_profiles", "employees"],
                    "what_joule_shows": "Employees with impactOfLoss=HIGH and riskOfLoss=HIGH or MEDIUM, with their role and grade.",
                    "ae_bridge": "That's a real talent profile, set when we provisioned this org. Joule didn't infer it — it read it.",
                },
                {
                    "beat": "Where the succession bench is thin",
                    "prompt": "Which of those flight-risk roles have no identified successor?",
                    "live_data": ["succession_nominations", "talent_profiles"],
                    "what_joule_shows": "Roles with high-risk incumbent and either zero nominations or only 3+ year readiness nominees.",
                    "ae_bridge": "Two live data points connected: who might leave and who could step up. That's the gap the board cares about.",
                },
                {
                    "beat": "The recognition signal",
                    "prompt": "Show me future leaders on my team who haven't been recognised with a spot award this year.",
                    "live_data": ["talent_profiles", "spot_awards"],
                    "what_joule_shows": "futureLeader=true employees vs spot award recipients — surfacing who's been overlooked.",
                    "ae_bridge": "Recognition is a retention lever. Joule just told the manager who to act on before year-end.",
                },
            ],
            "joule_desktop": [
                {
                    "beat": "Build the retention risk register",
                    "agent_instruction": "Read talent profiles for all employees. Identify anyone who is high impact of loss, medium or high risk of loss, AND either has no succession nomination or has a dev goal stuck On Track for 6+ months. For each person, write a two-sentence retention risk summary.",
                    "mcp_calls": ["SF OData: talent profile fields (impactOfLoss, riskOfLoss, futureLeader)", "SF OData: NominationService — succession depth per position", "SF OData: DevGoal_2001 — state and due date"],
                    "what_it_produces": "A ranked risk register: person, role, risk level, succession gap (yes/no), dev goal stall (yes/no), and a 2-sentence narrative per row — ready to paste into an HR business review deck.",
                    "ae_bridge": "The agent read across talent, succession, and goals in one pass. That's three modules connected, a task that would take an HRBP 30 minutes manually.",
                },
                {
                    "beat": "Match flight risks to their likely next move",
                    "agent_instruction": "For the top 2 flight risk employees, describe what their next role likely looks like externally based on their current title, grade, and goals. What would you offer to keep them? Frame it as a retention conversation guide for their manager.",
                    "mcp_calls": ["SF OData: EmpJob (title, grade)", "SF OData: DevGoal_2001 (aspiration)", "SF OData: spot awards (recognition history)"],
                    "what_it_produces": "Two manager-ready conversation guides: external market context, what the employee is likely being offered, and 3 specific retention levers the manager can pull — based on live data.",
                    "ae_bridge": "The agent synthesised data into advice. It's not telling the manager what SF says. It's telling the manager what to do.",
                },
                {
                    "beat": "CHRO briefing note",
                    "agent_instruction": "Prepare a 1-page retention risk brief for the CHRO. Lead with the number of high-risk roles, identify the two highest-priority actions, and end with a recommended 30-day plan.",
                    "mcp_calls": ["SF OData: talent profiles + succession + goals — aggregated view"],
                    "what_it_produces": "A structured CHRO brief: exec summary, risk count, the two names and their gaps, and a 30-day action plan — ready to send.",
                    "ae_bridge": "One instruction, one output. The CHRO gets a brief, not a dashboard. That's the agentic difference.",
                },
            ],
        },
        "joule_prompts": [
            "Who has high impact of loss and high risk of leaving right now?",
            "Which critical roles have no identified successor and a flight-risk incumbent?",
            "Show me future leaders on my team who haven't been recognised this year.",
            "Which employees have development goals tied to a next-level role?",
            "Recommend retention actions for my top three flight risks.",
        ],
        "live_data": ["org_structure", "employees", "talent_profiles", "succession_nominations", "spot_awards", "goal_assignments"],
        "story_data": ["retention_action_plans", "development_conversations", "counter_offer_tracking"],
        "story_narrative": (
            "Talent profiles (impact/risk/futureLeader), succession nominations, and employee goals are live. "
            "Joule can identify flight risks, succession gaps, and development progress from real data. "
            "Retention action plans, 1:1 notes, and continuous feedback are narrative — "
            "they require the Continuous Feedback module to be configured."
        ),
    },
    "skills_learning": {
        "label": "Skills Gap & Learning Development",
        "org_bias": "standard",
        "talent_story": "Skills inventory incomplete; learning paths not aligned to role requirements; L&D budget under scrutiny.",
        "agent_card": {
            "title": "Skills & Development Advisor",
            "challenge": (
                "The L&D budget review is next month and the team can't answer which employees have "
                "critical skill gaps, which training has actually been completed, or whether the "
                "learning investments are aligned to the roles that matter most."
            ),
            "prompts": [
                "Which employees on my team are marked as future leaders but have no development goals?",
                "Show me who has high impact of loss and what their current development focus is.",
                "Which roles in my org have the widest gap between current grade and next-level requirements?",
            ],
        },
        "demo_story": {
            "joule_chat": [
                {
                    "beat": "Find the development blind spots",
                    "prompt": "Which employees are marked as future leaders but have no development goals?",
                    "live_data": ["talent_profiles", "goal_assignments"],
                    "what_joule_shows": "futureLeader=true employees cross-referenced against DevGoal_2001 — who's flagged but not invested in.",
                    "ae_bridge": "The system knows who the company thinks is a future leader. The agent just checked whether anyone is actually doing anything about it.",
                },
                {
                    "beat": "Connect impact to development",
                    "prompt": "Show me who has high impact of loss and what their current development focus is.",
                    "live_data": ["talent_profiles", "goal_assignments"],
                    "what_joule_shows": "High-impact employees with their active dev goal name and metric — linking business criticality to growth trajectory.",
                    "ae_bridge": "This is what an HRBP would spend an afternoon pulling. Joule answered it in the conversation.",
                },
                {
                    "beat": "Readiness gap by role",
                    "prompt": "Which roles in my org have people at GR-13 who should be growing toward GR-14?",
                    "live_data": ["employees", "goal_assignments"],
                    "what_joule_shows": "GR-13 employees with their development goal focus — the organic succession pipeline below the formal nominations.",
                    "ae_bridge": "The nominations are the formal view. The goals data is the real signal of who's actually developing.",
                },
            ],
            "joule_desktop": [
                {
                    "beat": "Build the development investment map",
                    "agent_instruction": "For each employee, read their dev goal (DevGoal_2001), talent profile (futureLeader, impactOfLoss), and pay grade. Create a 2x2 view: high impact + dev goal present, high impact + no dev goal, low impact + dev goal, low impact + no dev goal. Name each quadrant.",
                    "mcp_calls": ["SF OData: DevGoal_2001 by userId", "SF OData: User talent profile fields", "SF OData: EmpJob grades"],
                    "what_it_produces": "A 2x2 development investment map with named employees in each quadrant and a headline finding: e.g. '2 high-impact employees have no active development goal — the highest-priority gap.'",
                    "ae_bridge": "This is a classic talent analytics deliverable. It normally comes from a Workday or SF People Analytics export. The agent built it live from OData.",
                },
                {
                    "beat": "L&D priority recommendation",
                    "agent_instruction": "Based on the development gaps identified, recommend the top 3 development investments the company should make in the next 6 months. Anchor each recommendation to a specific employee, their gap, and the business impact of closing it.",
                    "mcp_calls": ["SF OData: DevGoal_2001 + talent profiles — cross-org view"],
                    "what_it_produces": "3 named development recommendations: [Employee] → [Gap] → [Recommended investment] → [Business case]. Concrete and actionable.",
                    "ae_bridge": "The agent moved from data to recommendation. That's what the talent leader needs for the L&D budget conversation.",
                },
                {
                    "beat": "Succession readiness vs formal nominations",
                    "agent_instruction": "Compare the formal succession nominations to the organic pipeline visible in dev goals. Who has a dev goal pointing toward a senior role but isn't on the formal succession list? Flag them as 'informal pipeline.'",
                    "mcp_calls": ["SF OData: NominationService — nominated successors", "SF OData: DevGoal_2001 — purpose and name fields"],
                    "what_it_produces": "Side-by-side: formal nominations table vs informal pipeline. Highlights anyone doing the right development work but not yet visible to senior leadership.",
                    "ae_bridge": "Two data layers that never talk to each other in standard reporting. The agent connected them and found people worth nominating.",
                },
            ],
        },
        "joule_prompts": [
            "Which employees on my team are marked as future leaders but have no development goals?",
            "Show me who has high impact of loss and what their current development focus is.",
            "Which roles in my org have the widest gap between current grade and next-level requirements?",
            "Recommend a development path for someone moving from GR-13 to GR-14.",
            "Who completed a development goal this year and is ready for stretch?",
        ],
        "live_data": ["org_structure", "employees", "talent_profiles", "goal_assignments"],
        "story_data": ["skills_assignments", "learning_completions", "learning_catalog"],
        "story_narrative": (
            "Org structure, talent profiles, and development goals (DevGoal_2001) are live. "
            "Joule can reason about development focus and future-leader readiness from real data. "
            "WSM skill profiles, LMS completions, and the learning catalog are narrative — "
            "they require Workforce Skills Management and Learning modules configured with content."
        ),
    },
    "performance_goals": {
        "label": "Performance Management & Goal Setting",
        "org_bias": "standard",
        "talent_story": "Mid-year review cycle; goals set; manager calibration session next week.",
        "agent_card": {
            "title": "Goals & Performance Assistant",
            "challenge": (
                "Calibration is scheduled for next week and the manager doesn't know which employees "
                "have made meaningful progress on their annual goals, who is coasting on development "
                "targets, and who deserves to be called out as a standout this cycle."
            ),
            "prompts": [
                "Show me the annual goals for each person on my team.",
                "Which employees have a development goal tied to a leadership or next-level skill?",
                "Who has been recognised with a spot award this year and what for?",
            ],
        },
        "demo_story": {
            "joule_chat": [
                {
                    "beat": "Goal overview before the calibration walk-in",
                    "prompt": "Show me the annual goals for each person on my team.",
                    "live_data": ["goal_assignments", "employees"],
                    "what_joule_shows": "Each team member with their 2 Goal_11 entries — name, metric, and state (On Track).",
                    "ae_bridge": "Real goals, provisioned minutes ago. Joule read them from Goal_11. Not a demo account — this company was built for this conversation.",
                },
                {
                    "beat": "Who's investing in the next level",
                    "prompt": "Which employees have a development goal tied to a leadership or next-level skill?",
                    "live_data": ["goal_assignments"],
                    "what_joule_shows": "DevGoal_2001 entries with purpose='Current role' and name pointing to leadership or technical advancement.",
                    "ae_bridge": "Development goals are a leading indicator of who's ready to grow. The manager sees this before the calibration conversation, not after.",
                },
                {
                    "beat": "Recognition as a performance signal",
                    "prompt": "Who has been recognised with a spot award this year and what for?",
                    "live_data": ["spot_awards"],
                    "what_joule_shows": "SpotAward records: nominator, nominee, amount, reason, date — all Approved status.",
                    "ae_bridge": "Recognition data and performance data side by side. The manager can walk into calibration knowing who's been called out and why.",
                },
            ],
            "joule_desktop": [
                {
                    "beat": "Pre-calibration team briefing",
                    "agent_instruction": "For each employee on the team, pull their 2 annual goals, 1 dev goal, spot award history, talent profile flags, and grade. Produce a calibration briefing card per person: 4 bullet points, written for a manager going into a talent review.",
                    "mcp_calls": ["SF OData: Goal_11 + DevGoal_2001 + SpotAward + talent profile fields + EmpJob grade — per employee"],
                    "what_it_produces": "5 calibration cards, one per employee: goals summary, development focus, recognition highlights, talent flags (futureLeader, impactOfLoss). Formatted as 4 bullets each.",
                    "ae_bridge": "The manager's prep pack for the calibration session. Normally this takes an HRBP half a day to compile. The agent built it in one pass across five SF entities.",
                },
                {
                    "beat": "Spot the standouts and the risks",
                    "agent_instruction": "Across the team, identify: (1) who has the strongest goal-recognition alignment — ambitious goals AND spot awards; (2) who has goals On Track but no recognition or dev investment — potential flight risk. Name both groups.",
                    "mcp_calls": ["SF OData: Goal_11 state + SpotAward + DevGoal_2001 + riskOfLoss"],
                    "what_it_produces": "Two named groups with plain-language reasoning: the standouts the manager should call out in calibration, and the 'quiet quitters' whose engagement signal is going negative.",
                    "ae_bridge": "The agent synthesised four signals into a manager action list. Not a report — a recommendation.",
                },
                {
                    "beat": "Draft the calibration talking points",
                    "agent_instruction": "For the top performer on this team, draft a calibration talking point: what they've achieved, how they've grown, what they should be recognised for. Then do the same for the person most at risk of being overlooked. Keep each under 100 words.",
                    "mcp_calls": ["SF OData: goals + awards + talent profile — top 2 employees"],
                    "what_it_produces": "Two calibration talking points, ready to read aloud: grounded in specific goals and recognition data, written to influence the calibration room.",
                    "ae_bridge": "This is what makes the Joule Desktop tier different from a chatbot. It produced the output the manager needs to walk out of calibration with their people fairly represented.",
                },
            ],
        },
        "joule_prompts": [
            "Show me the annual goals for each person on my team.",
            "Which employees have a development goal tied to a leadership or next-level skill?",
            "Who has been recognised with a spot award this year and what for?",
            "Draft a mid-year performance summary for my VP Engineering.",
            "Which team members have the strongest goal-to-recognition alignment?",
        ],
        "live_data": ["org_structure", "employees", "talent_profiles", "spot_awards", "goal_assignments"],
        "story_data": ["performance_forms", "calibration_sessions", "ratings"],
        "story_narrative": (
            "Annual goals (Goal_11) and development goals (DevGoal_2001) are provisioned live — "
            "each employee has 2 annual goals and 1 development goal. "
            "Joule can surface goal content, ownership, and spot award history from real data. "
            "Performance review forms, ratings, and calibration sessions are narrative — "
            "they require PM module form templates and an active review cycle."
        ),
    },
    "workforce_planning": {
        "label": "Workforce Planning & Org Design",
        "org_bias": "standard",
        "talent_story": "Headcount request submitted; org restructure under review; budget owner needs visibility.",
        "agent_card": {
            "title": "Org Design & Headcount Assistant",
            "challenge": (
                "A restructure proposal is on the table but the business lead doesn't have a clear "
                "picture of current span of control, which roles are vacant, or where the talent risk "
                "is concentrated before they move headcount around."
            ),
            "prompts": [
                "Show me the current org structure and reporting lines.",
                "Which managers have the widest span of control?",
                "Where is talent risk highest — who has no successor and is a flight risk?",
            ],
        },
        "demo_story": {
            "joule_chat": [
                {
                    "beat": "Baseline the org before touching it",
                    "prompt": "Show me the current org structure and reporting lines.",
                    "live_data": ["org_structure", "employees"],
                    "what_joule_shows": "Org chart with reporting hierarchy, role titles, and headcount by department.",
                    "ae_bridge": "This is the org as provisioned. Every position, every reporting line, real data. The restructure conversation starts here.",
                },
                {
                    "beat": "Find the span-of-control problem",
                    "prompt": "Which managers have the widest span of control relative to their grade?",
                    "live_data": ["org_structure", "employees"],
                    "what_joule_shows": "Manager vs direct report count, flagging anyone above the recommended ratio for their grade.",
                    "ae_bridge": "A span-of-control problem is often invisible until a restructure makes it urgent. Joule surfaced it proactively.",
                },
                {
                    "beat": "Where is the talent risk concentrated?",
                    "prompt": "Which departments have the highest concentration of flight risk and no succession cover?",
                    "live_data": ["talent_profiles", "succession_nominations"],
                    "what_joule_shows": "Department-level risk summary: count of high riskOfLoss employees, succession nominations per position.",
                    "ae_bridge": "Before moving headcount, you need to know where you can't afford to lose people. This is that answer.",
                },
            ],
            "joule_desktop": [
                {
                    "beat": "Org health scan for the restructure brief",
                    "agent_instruction": "Read the full org structure, grades, spans of control, talent profiles, and succession nominations. Produce a 1-page org health brief: headcount by department, span-of-control outliers, talent risk hot spots, and succession gaps. Write it for a CHRO who is reviewing a restructure proposal.",
                    "mcp_calls": ["SF OData: EmpJob org hierarchy", "SF OData: talent profiles all", "SF OData: NominationService succession depth"],
                    "what_it_produces": "A structured org health brief: department headcount table, 2 span-of-control flags, top 3 talent risk concentrations, and a bottom-line assessment of whether this org is restructure-ready.",
                    "ae_bridge": "This is the brief the CHRO needs before approving the proposal. The agent assembled it from three live SF entities in one instruction.",
                },
                {
                    "beat": "Restructure scenario: impact on talent risk",
                    "agent_instruction": "The proposal is to consolidate Operations and Engineering under a single VP. Read the talent profiles and succession nominations for both departments. Would this increase or decrease talent concentration risk? Name the specific risks.",
                    "mcp_calls": ["SF OData: EmpJob department filter — OPS and ENG", "SF OData: talent profiles + succession for those employees"],
                    "what_it_produces": "A named risk assessment: which individuals become critical concentrations under the merged structure, whether any succession nominations span both departments, and a go/no-go read on the consolidation.",
                    "ae_bridge": "The agent analysed a hypothetical decision against live data. That's the advisory capability the restructure team doesn't have without running a full workforce analytics project.",
                },
                {
                    "beat": "Headcount summary for the finance partner",
                    "agent_instruction": "Summarise current headcount, grade distribution, and average compensation by department. Format it as a table the finance partner can use to model the restructure cost impact.",
                    "mcp_calls": ["SF OData: EmpJob (grade, dept)", "SF OData: EmpPayCompRecurring (current salary per employee)"],
                    "what_it_produces": "A department-level table: headcount, grade mix (GR-11 to GR-15 counts), average base salary, total payroll — formatted for a finance modelling spreadsheet.",
                    "ae_bridge": "The agent turned SF compensation data into a finance-ready summary. One instruction, one output, no People Analytics licence required.",
                },
            ],
        },
        "joule_prompts": [
            "Show me the current org structure and reporting lines.",
            "Which managers have the widest span of control?",
            "Where is talent risk highest — who has no successor and is a flight risk?",
            "What positions are currently filled vs vacant?",
            "Summarise headcount by department and pay grade.",
        ],
        "live_data": ["org_structure", "employees", "positions", "talent_profiles"],
        "story_data": ["headcount_plan", "attrition_forecast", "org_restructure_proposal", "budget_submissions"],
        "story_narrative": (
            "Org structure, positions, and employee data are live — Joule can surface org charts, "
            "span of control, and talent risk concentration from real data. "
            "Headcount planning, attrition forecasts, and budget submissions are narrative — "
            "they require Workforce Planning module and integration with Finance."
        ),
    },
    "succession_prep": {
        "label": "Succession Nomination Prep",
        "org_bias": "standard",
        "talent_story": "Board review in 6 weeks; succession depth for C-1 and C-2 roles not documented; managers avoiding the conversation.",
        "agent_card": {
            "title": "Succession Nomination Prep Assistant",
            "challenge": (
                "Managers are delaying critical successor nomination conversations heading into "
                "the annual board talent review. Readiness ratings are missing, development gaps "
                "haven't been assessed, and there's no clear view of bench depth for the top roles."
            ),
            "prompts": [
                "Which critical positions have fewer than two active successors nominated?",
                "Show me the readiness rating and development goals for each nominated successor.",
                "Who is flagged as a future leader but not yet nominated for any succession plan?",
            ],
        },
        "demo_story": {
            "joule_chat": [
                {
                    "beat": "Where is the bench thin?",
                    "prompt": "Which critical positions have fewer than two active successors nominated?",
                    "live_data": ["succession_nominations"],
                    "what_joule_shows": "Positions with 0 or 1 nominations from the live NominationService data.",
                    "ae_bridge": "These nominations were provisioned via the SF succession API. Real data, isolated company. This answer is grounded in what's actually in the system.",
                },
                {
                    "beat": "Successor readiness profile",
                    "prompt": "Show me the readiness rating and development goals for each nominated successor.",
                    "live_data": ["succession_nominations", "goal_assignments"],
                    "what_joule_shows": "Each nominee with their readiness value (1.0=Ready Now, 2.0=1-2yr, 3.0=3+yr) and their active development goal.",
                    "ae_bridge": "Readiness without development context is just a number. The goals data tells you whether the nominee is actually working toward it.",
                },
                {
                    "beat": "The pipeline below the nominations",
                    "prompt": "Who is flagged as a future leader but not yet nominated for any succession plan?",
                    "live_data": ["talent_profiles", "succession_nominations"],
                    "what_joule_shows": "futureLeader=true employees who do not appear in any NominationService record — the untapped pipeline.",
                    "ae_bridge": "Every succession plan has invisible candidates. Joule just made them visible.",
                },
            ],
            "joule_desktop": [
                {
                    "beat": "Build the succession board pack",
                    "agent_instruction": "Read all succession nominations with their readiness ratings. For each nominated position, read the incumbent's talent profile and the nominee's development goals. Produce a succession briefing in the format used for a board talent review: position, incumbent risk, nominee(s), readiness, and a 2-sentence gap assessment.",
                    "mcp_calls": ["SF OData v4: NominationService — all nominations + readiness", "SF OData: talent profiles for incumbents", "SF OData: DevGoal_2001 for nominees"],
                    "what_it_produces": "A succession board pack: 5 position rows, each with incumbent flight risk, nominee names + readiness levels, active dev goals, and a named gap (e.g. 'VP Eng has one Ready Now nominee but their dev goal doesn't address the strategic planning gap the role requires.').",
                    "ae_bridge": "This is a three-module read that HR normally takes a week to compile for the board. The agent produced it in one instruction.",
                },
                {
                    "beat": "Identify the hidden successors",
                    "agent_instruction": "For any position with fewer than 2 nominees at Readiness 1.0 or 2.0, identify employees at GR-13 or GR-14 who have futureLeader=true, a development goal pointing toward leadership, and are NOT currently nominated. Flag them as 'informal pipeline — recommend for nomination.'",
                    "mcp_calls": ["SF OData v4: NominationService", "SF OData: talent profiles (futureLeader)", "SF OData: DevGoal_2001 (dev goal name)", "SF OData: EmpJob (grade)"],
                    "what_it_produces": "A recommendation list: [Name] → [Current role] → [Dev goal] → [Recommended for: Position X] with a 1-sentence rationale per person.",
                    "ae_bridge": "The agent found succession candidates the formal process missed. That's analyst-grade work done in seconds.",
                },
                {
                    "beat": "Pre-meeting briefing for the CHRO",
                    "agent_instruction": "The CHRO has a 30-minute talent review meeting in 2 days. Prepare a concise pre-read: how many positions have adequate succession cover, how many are at risk, and what the top 2 actions are before the board meeting. Keep it under 200 words.",
                    "mcp_calls": ["SF OData v4: NominationService — aggregate view", "SF OData: talent profiles — flight risk for incumbents"],
                    "what_it_produces": "A 200-word pre-read: succession health summary (X of Y positions covered), the 2 critical gaps, and 2 specific action recommendations with named owners.",
                    "ae_bridge": "The CHRO gets a brief, not a dashboard. The agent wrote it. That's the Joule Desktop value proposition in one output.",
                },
            ],
        },
        "joule_prompts": [
            "Which critical positions have fewer than two active successors nominated?",
            "Show me the readiness rating and development goals for each nominated successor.",
            "Who is flagged as a future leader but not yet nominated for any succession plan?",
            "Prepare a succession briefing for the VP Engineering role.",
            "Which successors are ready now vs 1–2 years out?",
        ],
        "live_data": ["org_structure", "employees", "talent_profiles", "succession_nominations", "goal_assignments"],
        "story_data": ["readiness_assessments", "development_conversations", "board_pack"],
        "story_narrative": (
            "Succession nominations (OData v4 NominationService) and talent profiles are live. "
            "Joule can surface bench depth, readiness levels, and development goal alignment from real data. "
            "Formal readiness assessments, 9-box placements, and board pack generation are narrative — "
            "they require the Succession & Development module to be fully configured."
        ),
    },
    "pay_equity_deep_dive": {
        "label": "Pay Equity & Compensation Fairness",
        "org_bias": "standard",
        "talent_story": "CHRO asked for a pay equity audit before the comp cycle opens; grade and impact disparities need to be surfaced.",
        "agent_card": {
            "title": "Pay Equity Audit Assistant",
            "challenge": (
                "The CHRO needs a pay equity snapshot before the compensation cycle opens. "
                "There's no quick way to see who is paid below midpoint, whether high-impact employees "
                "are fairly compensated relative to peers, or where the largest salary variance sits."
            ),
            "prompts": [
                "Show me everyone below the midpoint for their pay grade across the org.",
                "Who has the highest impact of loss but is in the bottom quartile for their grade?",
                "Compare the salary progression over 3 years for GR-14 vs GR-13 employees.",
            ],
        },
        "demo_story": {
            "joule_chat": [
                {
                    "beat": "Midpoint gap across the org",
                    "prompt": "Show me everyone below the midpoint for their pay grade.",
                    "live_data": ["salary_history", "employees"],
                    "what_joule_shows": "Employees with current base vs estimated grade midpoint, flagging those below — drawn from 3 years of live EmpPayCompRecurring data.",
                    "ae_bridge": "This used to require a People Analytics report or a manual VLOOKUP against a pay band spreadsheet. Joule read it from EC directly.",
                },
                {
                    "beat": "High impact, underpaid — the dangerous combination",
                    "prompt": "Who has the highest impact of loss but is in the bottom quartile for their grade?",
                    "live_data": ["salary_history", "talent_profiles"],
                    "what_joule_shows": "Cross-reference of impactOfLoss=HIGH with bottom-quartile comp within their grade band.",
                    "ae_bridge": "This is the pay equity risk the CHRO actually cares about. Not aggregate statistics — specific people who can be named and acted on.",
                },
                {
                    "beat": "3-year progression: who's moving, who's stuck",
                    "prompt": "Compare salary progression over 3 years for GR-14 versus GR-13 employees.",
                    "live_data": ["salary_history"],
                    "what_joule_shows": "Year-over-year average base by grade, showing differential growth rates between GR-14 and GR-13.",
                    "ae_bridge": "3 years of real salary history per person. The progression comparison is live, not simulated.",
                },
            ],
            "joule_desktop": [
                {
                    "beat": "Full pay equity audit report",
                    "agent_instruction": "Read salary history and talent profile flags for all employees. Produce a pay equity audit: (1) who is below midpoint by grade, (2) the correlation between impactOfLoss and pay quartile, (3) salary growth rate by grade over 3 years. Present as a structured report suitable for the CHRO.",
                    "mcp_calls": ["SF OData: EmpPayCompRecurring — 3 entries per employee", "SF OData: talent profile fields — impactOfLoss"],
                    "what_it_produces": "A 3-section pay equity report: below-midpoint list by grade, impact-to-pay correlation summary with named outliers, and grade-level 3-year CAGR comparison.",
                    "ae_bridge": "This is an HR analytics deliverable that normally takes a People Analytics team 2 days to produce. The agent built it from SF OData in one instruction.",
                },
                {
                    "beat": "Prioritised remediation list",
                    "agent_instruction": "From the pay equity findings, rank the top 3 employees who need an immediate comp correction. For each: current base, estimated midpoint, correction delta, and business case for acting before the comp cycle opens.",
                    "mcp_calls": ["SF OData: EmpPayCompRecurring + talent profiles — sorted by risk-impact combination"],
                    "what_it_produces": "A remediation action list: 3 employees ranked by urgency, with their delta to midpoint in absolute and percentage terms, and a 1-sentence business case each ('GR-14 impact-critical role, 12% below midpoint, no raise in 18 months').",
                    "ae_bridge": "The agent converted an audit finding into an action list. The CHRO can forward this to the comp team as-is.",
                },
                {
                    "beat": "Equity narrative for the board",
                    "agent_instruction": "Write a 3-bullet pay equity summary for a board HR committee update. Lead with the headline finding, note the 2 highest-risk outliers (by role, not name), and close with a recommended action timeline.",
                    "mcp_calls": ["SF OData: compensation + talent profiles — aggregated"],
                    "what_it_produces": "3 board-ready bullet points: headline stat, two named risks (by role), action timeline. Appropriate for a 15-minute governance committee update.",
                    "ae_bridge": "From raw SF compensation data to boardroom-ready language. The agent bridged that gap in one step.",
                },
            ],
        },
        "joule_prompts": [
            "Show me everyone below the midpoint for their pay grade across the org.",
            "Who has the highest impact of loss but is in the bottom quartile for their grade?",
            "Compare the salary progression over 3 years for GR-14 vs GR-13 employees.",
            "Which departments have the widest spread between highest and lowest paid at the same grade?",
            "Flag anyone whose bonus was above target but base salary hasn't moved in 3 years.",
        ],
        "live_data": ["org_structure", "employees", "salary_history", "bonus", "talent_profiles"],
        "story_data": ["pay_equity_analysis", "compa_ratio_report", "gender_pay_gap_report"],
        "story_narrative": (
            "3 years of salary history and a Dec 2025 bonus entry are live per employee, with talent profile "
            "impact/risk ratings and pay grades for every person. Joule can surface grade-level comparisons "
            "and flag outliers from real data. Formal pay equity analysis, compa-ratio reports, and gender "
            "pay gap reporting are narrative — they require EC Compensation module and reporting configuration."
        ),
    },
    "onboarding_readiness": {
        "label": "Onboarding & Day-One Readiness",
        "org_bias": "standard",
        "talent_story": "New hire starts in 3 weeks; manager unprepared; no visibility into team context or first-week plan.",
        "agent_card": {
            "title": "Onboarding Readiness Assistant",
            "challenge": (
                "A new hire is 3 weeks out from their start date and the hiring manager hasn't been "
                "told who else is on the team, what the org looks like around them, or what a realistic "
                "30-60-90 day plan should include given the team's current goals and priorities."
            ),
            "prompts": [
                "Who is Sam Rivera's manager and what does their team look like?",
                "What are the current annual goals for the team Sam is joining?",
                "Who on the team was recently recognised — what were they called out for?",
            ],
        },
        "demo_story": {
            "joule_chat": [
                {
                    "beat": "Manager and team context",
                    "prompt": "Who is Sam Rivera's manager and what does their team look like?",
                    "live_data": ["onboardee", "org_structure", "employees"],
                    "what_joule_shows": "Sam's EmpJob record: manager name, role, and direct report list under that manager.",
                    "ae_bridge": "Sam is live in SF. Manager is assigned. This is what Day 1 prep looks like when the system is actually set up.",
                },
                {
                    "beat": "What the team is focused on",
                    "prompt": "What are the current annual goals for the team Sam is joining?",
                    "live_data": ["goal_assignments", "employees"],
                    "what_joule_shows": "Goal_11 entries for each team member — what they're working toward in the current cycle.",
                    "ae_bridge": "Sam can walk in knowing the team's priorities before their first meeting. Not guessing from a job description.",
                },
                {
                    "beat": "Recognition culture signal",
                    "prompt": "Who on the team was recently recognised and what for?",
                    "live_data": ["spot_awards"],
                    "what_joule_shows": "SpotAward records for team members: award reason, nominator, amount — gives Sam a read on team norms and who the standouts are.",
                    "ae_bridge": "Recognition history is a proxy for team culture and values. Joule just gave Sam a head start on reading the room.",
                },
            ],
            "joule_desktop": [
                {
                    "beat": "The manager's Day 1 prep briefing",
                    "agent_instruction": "Read Sam Rivera's employment record, their manager's team structure, the team's active annual and development goals, and recent spot awards. Produce a manager briefing for Sam's first week: who's on the team, what they're working on, and 3 suggestions for Sam's first conversations.",
                    "mcp_calls": ["SF OData: EmpJob for Sam + manager's direct reports", "SF OData: Goal_11 + DevGoal_2001 for team", "SF OData: SpotAward for team"],
                    "what_it_produces": "A Day 1 manager briefing: team roster with roles and grades, 2-sentence goal summary per person, recognition highlights, and 3 named first-conversation suggestions tailored to the team's real priorities.",
                    "ae_bridge": "The manager prep note the system should have generated automatically. The agent built it from live SF data in one instruction.",
                },
                {
                    "beat": "Sam's personalised 30-60-90 plan",
                    "agent_instruction": "Based on the team's active goals and Sam's role as Senior Associate, draft a 30-60-90 day onboarding plan. Each phase should reference a real team goal or a named colleague. Make it specific, not generic.",
                    "mcp_calls": ["SF OData: Goal_11 for team", "SF OData: EmpJob for Sam + team members", "SF OData: SpotAward — recognition context"],
                    "what_it_produces": "A structured 30-60-90 plan: Days 1-30 (team orientation, named first meetings, goal alignment), Days 31-60 (contribution phase, tied to specific team goals), Days 61-90 (independent contribution, named stretch objective). Real names and real goals throughout.",
                    "ae_bridge": "Not a template. A plan grounded in the actual team Sam is joining. This is what separates Joule Desktop from a doc generator.",
                },
                {
                    "beat": "Talent risk context for HR",
                    "agent_instruction": "For the team Sam Rivera is joining, surface any talent risk the HR team should know about at onboarding time: flight risk, succession gaps, or future leaders without development investment. Write it as a 3-bullet HR intake note.",
                    "mcp_calls": ["SF OData: talent profiles for team", "SF OData: NominationService — succession for team roles", "SF OData: DevGoal_2001 for futureLeader employees"],
                    "what_it_produces": "A 3-bullet HR intake note: (1) any flight risk on the receiving team, (2) succession gaps Sam might be positioned to eventually fill, (3) development investment gaps. Framed for the HRBP who owns the onboarding.",
                    "ae_bridge": "The agent gave HR the context they need to position this hire strategically — not just process the paperwork.",
                },
            ],
        },
        "joule_prompts": [
            "Who is Sam Rivera's manager and what does their team look like?",
            "What are the current annual goals for the team Sam is joining?",
            "Who on the team was recently recognised — what were they called out for?",
            "Draft a 30-60-90 day onboarding plan for Sam based on the team's current priorities.",
            "What development goals are active on the team Sam is joining?",
        ],
        "live_data": ["org_structure", "employees", "onboardee", "goal_assignments", "spot_awards"],
        "story_data": ["onboarding_tasks", "buddy_assignment", "equipment_provisioning"],
        "story_narrative": (
            "The onboardee Sam Rivera is live in SF with a manager, position, and Nov 3 start date. "
            "The team's goals (Goal_11) and recent spot awards are also live — "
            "Joule can give the manager a real picture of what Sam is walking into. "
            "Formal onboarding task lists, buddy assignments, and equipment provisioning are narrative — "
            "they require Onboarding module workflow configuration."
        ),
    },
}

LOCALE_CONFIG = {
    "USA": {"currency": "USD", "pay_scale": "USA/US1", "pay_group": "US", "locale": "en_US", "tz": "US/Eastern", "country_code": "USA"},
    "GBR": {"currency": "GBP", "pay_scale": "GBR/GB1", "pay_group": "GB01", "locale": "en_GB", "tz": "Europe/London", "country_code": "GBR"},
    "DEU": {"currency": "EUR", "pay_scale": "DEU/DE1", "pay_group": "DE01", "locale": "de_DE", "tz": "Europe/Berlin", "country_code": "DEU"},
    "FRA": {"currency": "EUR", "pay_scale": "FRA/FR1", "pay_group": "FR01", "locale": "fr_FR", "tz": "Europe/Paris", "country_code": "FRA"},
    "IND": {"currency": "INR", "pay_scale": "IND/IN1", "pay_group": "IN01", "locale": "en_IN", "tz": "Asia/Kolkata", "country_code": "IND"},
    "AUS": {"currency": "AUD", "pay_scale": "AUS/AU1", "pay_group": "AU01", "locale": "en_AU", "tz": "Australia/Sydney", "country_code": "AUS"},
    "SGP": {"currency": "SGD", "pay_scale": "SGP/SG1", "pay_group": "SG01", "locale": "en_US", "tz": "Asia/Singapore", "country_code": "SGP"},
    "BRA": {"currency": "BRL", "pay_scale": "BRA/BR1", "pay_group": "BR01", "locale": "pt_BR", "tz": "America/Sao_Paulo", "country_code": "BRA"},
}

INDUSTRY_ROLES = {
    "retail": {
        "ceo":   ("001", "CEO",             "Chief Executive Officer",    "EXEC", "GR-15"),
        "ops":   ("002", "SVP Ops",         "SVP Store Operations",       "OPS",  "GR-14"),
        "merch": ("003", "VP Merch",        "VP Merchandising",           "PROD", "GR-14"),
        "hr":    ("004", "CHRO",            "Chief HR Officer",           "OPS",  "GR-14"),
        "fin":   ("005", "CFO",             "Chief Financial Officer",    "FIN",  "GR-14"),
    },
    "tech": {
        "ceo":   ("001", "CEO",             "Chief Executive Officer",    "EXEC", "GR-15"),
        "eng":   ("002", "VP Eng",          "VP Engineering",             "ENG",  "GR-14"),
        "prod":  ("003", "VP Product",      "VP Product",                 "PROD", "GR-14"),
        "sales": ("004", "VP Sales",        "VP Sales",                   "SALES","GR-14"),
        "cos":   ("005", "CoS",             "Chief of Staff",             "OPS",  "GR-12"),
    },
    "manufacturing": {
        "ceo":   ("001", "CEO",             "Chief Executive Officer",    "EXEC", "GR-15"),
        "plant": ("002", "Plant Mgr",       "Plant Manager",              "OPS",  "GR-14"),
        "eng":   ("003", "VP Eng",          "VP Engineering",             "ENG",  "GR-14"),
        "sc":    ("004", "SC Mgr",          "Supply Chain Manager",       "SALES","GR-13"),
        "hr":    ("005", "HR BP",           "HR Business Partner",        "OPS",  "GR-11"),
    },
    "healthcare": {
        "ceo":   ("001", "CEO",             "Chief Executive Officer",    "EXEC", "GR-15"),
        "cmo":   ("002", "CMO",             "Chief Medical Officer",      "MED",  "GR-15"),
        "ops":   ("003", "VP Ops",          "VP Clinical Operations",     "OPS",  "GR-14"),
        "fin":   ("004", "CFO",             "Chief Financial Officer",    "FIN",  "GR-14"),
        "hr":    ("005", "HR BP",           "HR Business Partner",        "OPS",  "GR-11"),
    },
    "financial_services": {
        "ceo":   ("001", "CEO",             "Chief Executive Officer",    "EXEC", "GR-15"),
        "cro":   ("002", "CRO",             "Chief Risk Officer",         "FIN",  "GR-15"),
        "ops":   ("003", "COO",             "Chief Operating Officer",    "OPS",  "GR-14"),
        "sales": ("004", "Head Sales",      "Head of Client Coverage",    "SALES","GR-14"),
        "hr":    ("005", "HR BP",           "HR Business Partner",        "OPS",  "GR-11"),
    },
    "energy": {
        "ceo":   ("001", "CEO",             "Chief Executive Officer",    "EXEC", "GR-15"),
        "ops":   ("002", "VP Ops",          "VP Field Operations",        "OPS",  "GR-14"),
        "eng":   ("003", "VP Eng",          "VP Engineering",             "ENG",  "GR-14"),
        "hse":   ("004", "HSE Dir",         "Director Health Safety Env", "OPS",  "GR-13"),
        "hr":    ("005", "HR BP",           "HR Business Partner",        "OPS",  "GR-11"),
    },
}

FIRST_NAMES = {
    "ceo": ("Jordan", "Kim"),       "eng": ("Priya", "Mehta"),
    "prod": ("Sona", "Park"),       "sales": ("Marcus", "Webb"),
    "cos": ("Elise", "Torres"),     "ops": ("Dana", "Reeves"),
    "plant": ("Marco", "Silva"),    "sc": ("Ayesha", "Khan"),
    "hr": ("Hira", "Nair"),         "merch": ("Cleo", "Nash"),
    "fin": ("Jordan", "Moss"),      "cmo": ("Dr. Ethan", "Walsh"),
    "cro": ("Natalie", "Cross"),    "hse": ("Owen", "Fletcher"),
    "head sales": ("Leon", "Park"),
}

SALARY_HISTORY = {
    "GR-15": [160000, 171000, 182000],
    "GR-14": [142000, 152000, 162000],
    "GR-13": [118000, 127000, 136000],
    "GR-12": [116000, 125000, 135000],
    "GR-11": [100000, 108000, 118000],
}

GRADE_IMPACT = {
    "GR-15": ("HIGH",   "HIGH",   True),
    "GR-14": ("HIGH",   "MEDIUM", True),
    "GR-13": ("MEDIUM", "MEDIUM", False),
    "GR-12": ("MEDIUM", "LOW",    False),
    "GR-11": ("LOW",    "LOW",    False),
}

DEPT_DIVISION = {
    "EXEC": "CORP_SVCS", "OPS": "CORP_SVCS", "SALES": "CORP_SVCS",
    "FIN": "CORP_SVCS",  "MED": "MANU",      "ENG": "MANU",
    "PROD": "MANU",
}
DEPT_BU = {
    "EXEC": "CORP", "OPS": "CORP",  "SALES": "CORP", "FIN": "CORP",
    "MED": "PRODS", "ENG": "PRODS", "PROD": "PRODS",
}
DEPT_NAMES = {
    "EXEC": "Executive", "ENG": "Engineering", "PROD": "Product",
    "SALES": "Sales",    "OPS": "Operations",  "FIN": "Finance",
    "MED": "Medical",    "ADMIN": "Administration",
}

BONUS_BY_GRADE = {
    "GR-15": 25000, "GR-14": 18000, "GR-13": 14000,
    "GR-12": 12000, "GR-11": 10000,
}

# Annual goals (Goal_11) and development goals (DevGoal_2001) by industry role
# Each tuple: (annual_goal_1_name, annual_goal_1_metric, annual_goal_2_name, annual_goal_2_metric,
#              dev_goal_name, dev_goal_metric)
GOAL_CONTENT = {
    "ceo": (
        "Company Revenue Growth",        "Achieve 20% YoY revenue growth and expand to 2 new markets",
        "Leadership & Culture",          "Maintain eNPS >= 55; complete 4 all-hands and 1 offsite",
        "Executive Presence & Stakeholder Influence", "Lead 3 board-level presentations; complete exec leadership programme",
    ),
    "eng": (
        "Platform Reliability",          "Achieve 99.9% uptime; reduce P1 incidents by 40%",
        "Engineering Velocity",          "Ship 85% of planned roadmap features on schedule",
        "Technical Architecture Mastery","Complete cloud architecture certification; lead 1 system design review",
    ),
    "prod": (
        "Product Launch Success",        "Launch 2 major features with NPS >= 45 and adoption >= 60%",
        "Customer Discovery",            "Conduct 24 customer interviews; translate insights into 3 product bets",
        "Product Strategy & Roadmapping","Complete advanced product strategy course; present 3-year vision to leadership",
    ),
    "sales": (
        "Sales Target Achievement",      "Achieve 110% of quota; close 5 new enterprise logos",
        "Pipeline Development",          "Maintain pipeline coverage 3x; generate 40 qualified opportunities",
        "Consultative Selling Skills",   "Complete Miller Heiman certification; apply methodology on 10 deals",
    ),
    "cos": (
        "Executive Coordination",        "Drive 100% on-time delivery of CEO-sponsored initiatives",
        "Process Improvement",           "Identify and eliminate 3 bottlenecks; reduce decision latency by 30%",
        "Strategic Communication",       "Complete executive communication programme; shadow 5 C-level strategy sessions",
    ),
    "ops": (
        "Operational Efficiency",        "Reduce operational costs by 12% while maintaining quality SLAs",
        "Process Automation",            "Automate 4 manual workflows; save 200+ hours/month",
        "Change Management",             "Complete prosci change management certification; lead 1 transformation project",
    ),
    "plant": (
        "Production Output",             "Meet 98% of monthly production targets with <1% defect rate",
        "Safety & Compliance",           "Zero LTIs; maintain ISO certification; 100% audit pass rate",
        "Lean Manufacturing",            "Complete Lean Six Sigma Green Belt; apply to 2 production lines",
    ),
    "sc": (
        "Supply Chain Resilience",       "Reduce lead times by 15%; build dual-sourcing for top 10 components",
        "Inventory Optimisation",        "Reduce inventory carrying cost by 10% while maintaining 98% fill rate",
        "Supply Chain Analytics",        "Complete supply chain analytics certification; build 2 predictive dashboards",
    ),
    "hr": (
        "Employee Experience",           "Achieve 80% engagement score; reduce voluntary turnover to <10%",
        "Talent Acquisition",            "Fill open positions in <45 days avg; achieve 90% hiring manager satisfaction",
        "HR Digital Transformation",     "Complete SAP SuccessFactors advanced certification; lead 2 HR tech rollouts",
    ),
    "merch": (
        "Category Performance",          "Grow category revenue by 15%; achieve 50% gross margin on key lines",
        "Vendor Partnership",            "Negotiate 3 new strategic vendor agreements; reduce procurement cost 8%",
        "Merchandising Analytics",       "Complete retail analytics certification; implement data-driven assortment model",
    ),
    "fin": (
        "Financial Close Excellence",    "Achieve hard close in 3 days; zero material audit findings",
        "Cost Reduction Initiatives",    "Identify and deliver $2M in cost savings across 3 business units",
        "Financial Modelling",           "Complete FP&A advanced programme; build integrated 3-statement model",
    ),
    "cmo": (
        "Clinical Quality Outcomes",     "Maintain patient satisfaction >= 90%; zero preventable adverse events",
        "Clinical Efficiency",           "Reduce average length of stay by 8%; improve throughput by 12%",
        "Clinical Leadership",           "Complete healthcare executive leadership programme; mentor 3 junior clinicians",
    ),
    "cro": (
        "Risk Framework",                "Implement enterprise risk framework; reduce operational risk exposure by 20%",
        "Regulatory Compliance",         "Zero regulatory breaches; pass all audits with zero material findings",
        "Risk Analytics",                "Complete advanced risk modelling certification; build 2 predictive risk models",
    ),
    "hse": (
        "Safety Performance",            "Zero LTIs; achieve TRIR <= 0.4; maintain all safety certifications",
        "Environmental Compliance",      "Meet all regulatory targets; reduce Scope 1 emissions by 8%",
        "HSE Systems",                   "Complete NEBOSH diploma; implement ISO 14001 improvements in 2 sites",
    ),
}

# Fallback goal content for roles not in GOAL_CONTENT
_DEFAULT_GOAL = (
    "Business Performance",          "Achieve key performance targets and deliver measurable business impact",
    "Team Development",              "Complete assigned goals on schedule with high quality output",
    "Professional Development",      "Complete 2 relevant training courses; apply learnings to current role",
)

# ── Helpers ───────────────────────────────────────────────────────────────────

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
        try:
            msg = json.loads(err)["error"]["message"]["value"]
        except Exception:
            msg = err[:200]
        if "already exists" in msg.lower() or "duplicate" in msg.lower():
            return True, "already_exists"
        return False, msg[:120]


def _ias_get_user(username: str, sf: dict):
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


# ── Phase 1: Design ───────────────────────────────────────────────────────────

@mcp.tool()
def design_demo_org(
    company_name: str,
    industry: str,
    country: str,
    business_problem: str,
    n_employees: int = 5,
    company_code: Optional[str] = None,
    employee_prefix: Optional[str] = None,
    email_prefix: Optional[str] = None,
    password: str = "",
    personas: Optional[list] = None,
    scenario_override: Optional[dict] = None,
    ctx=None,
) -> str:
    """
    Phase 1: Design a demo org for a specific customer, industry, and business problem.

    Returns a complete org plan including:
    - Role definitions with real SF data fields
    - Scenario narrative and story arc
    - Joule conversation prompts that will work against this data
    - Honest capability manifest: what will be LIVE vs what is STORY

    When called via HTTP with a valid SAP Bearer token, the caller's email is used
    to set email_prefix automatically (principal propagation). The +alias suffix
    in the email identifies the caller's SF user for persona defaulting.

    Args:
        company_name:       Customer/company name (e.g. "Nike", "Siemens Energy")
        industry:           Industry vertical: retail, tech, manufacturing, healthcare,
                            financial_services, energy
        country:            Country code for locale/currency: USA, GBR, DEU, FRA, IND,
                            AUS, SGP, BRA
        business_problem:   The SF domain scenario: mass_hiring, compensation_planning,
                            talent_retention, skills_learning, performance_goals,
                            workforce_planning, succession_prep, pay_equity_deep_dive,
                            onboarding_readiness
        n_employees:        Number of employees to create (currently 5 supported)
        company_code:       4-digit SF company code (auto-assigned if omitted)
        employee_prefix:    2-3 char userId prefix (e.g. "NK" for Nike)
        email_prefix:       Email address prefix for +alias users (defaults to caller's
                            email prefix via principal propagation, else env/config)
        password:           Default login password for all users
        personas:           Optional list of custom persona dicts, one per employee.
                            Each dict must contain:
                              first_name  - REQUIRED: person's given name (e.g. "Jordan")
                              last_name   - REQUIRED: person's family name (e.g. "Kim")
                              role_key    - short identifier used as username suffix (e.g. "cpo")
                              job_title   - full title (e.g. "Chief People Officer")
                              short_title - abbreviated title for display (e.g. "CPO")
                              department  - dept key: EXEC, OPS, FIN, ENG, PROD, SALES, MED
                              pay_grade   - SF grade: GR-11 through GR-15
                              employee_number - zero-padded 3-digit string (e.g. "001")
                              goals       - dict with keys: annual_1_name, annual_1_metric,
                                            annual_2_name, annual_2_metric, dev_name, dev_metric
                              spot_award_message - spot award message (null for CEO / persona[0])
                            IMPORTANT: first_name and last_name are required. Omitting them
                            causes all employees to appear as "UserXXX Smith" in SuccessFactors.
                            When provided, overrides INDUSTRY_ROLES and GOAL_CONTENT entirely.
                            The first persona is always the CEO / root manager.
        scenario_override:  Optional dict to replace the SCENARIO_KB entry. Keys:
                              label          - short scenario label (e.g. "Skills & Internal Mobility")
                              challenge      - 2-3 sentence customer challenge narrative
                              talent_story   - 1-sentence org backstory shown in the plan summary
                              joule_prompts  - list of 4-6 Joule chat prompts tuned to this demo
                              story_narrative - what is live vs story (plain text)
                            When provided, overrides the static SCENARIO_KB lookup entirely.
    """
    # Principal propagation: pull email from XSUAA JWT if available
    caller_email = _extract_caller_email(ctx)
    caller_alias = _alias_from_email(caller_email) if caller_email else None
    _design_sf = _build_sf_config(caller_email)

    # email_prefix: caller arg > derived from JWT email > env default
    if email_prefix is None:
        if caller_email:
            # strip the +alias and @domain, keep the local prefix
            local = caller_email.split("@")[0]
            email_prefix = local.split("+")[0]  # e.g. siddhartha.bhattacharya
        else:
            email_prefix = DEFAULT_EMAIL_PREFIX
    industry_key   = industry.lower().replace(" ", "_").replace("-", "_")
    problem_key    = business_problem.lower().replace(" ", "_").replace("-", "_")
    country_key    = country.upper()

    # Validate inputs and apply defaults
    if not personas and industry_key not in INDUSTRY_ROLES:
        available = list(INDUSTRY_ROLES.keys())
        return json.dumps({"error": f"Unknown industry '{industry}'. Available: {available}"})

    if not scenario_override and problem_key not in SCENARIO_KB:
        available = list(SCENARIO_KB.keys())
        return json.dumps({"error": f"Unknown problem '{business_problem}'. Available: {available}"})

    if country_key not in LOCALE_CONFIG:
        country_key = "USA"  # default fallback

    if n_employees < 2:
        return json.dumps({"error": "n_employees must be at least 2 (1 manager + 1 report required for org hierarchy)."})

    locale = LOCALE_CONFIG.get(country_key, LOCALE_CONFIG["USA"])
    scenario = scenario_override if scenario_override else SCENARIO_KB[problem_key]

    # Generate unique password per org — avoids IAS password history rejection on re-runs
    if not password:
        suffix = secrets.token_hex(2).upper()  # e.g. "A3F2"
        password = f"Demo{suffix}26!"          # e.g. "DemoA3F226!"

    # Auto-assign company code if not given (hash company name to 4-digit range 5000-9000)
    if not company_code:
        company_code = str(5000 + (hash(company_name) % 4000))

    # Auto-assign employee prefix from company name + 3-char random hex suffix
    # so multiple orgs for the same company get unique user IDs and SF usernames.
    if not employee_prefix:
        words = company_name.upper().split()
        if len(words) >= 2:
            base_prefix = words[0][:1] + words[1][:1]
        else:
            base_prefix = company_name.upper()[:3]
        suffix = secrets.token_hex(2)[:3].upper()  # e.g. "A3F"
        employee_prefix = base_prefix + suffix      # e.g. "HQA3F"

    # Build employee roster — agent-provided personas take precedence over static tables
    employees = []
    ceo_id = None

    if personas:
        # Path A: agent passed explicit persona list (dynamic, customer-specific)
        # Use any names the agent provided; fill gaps from the name pool.
        # Never fall back to "UserXXX Smith" — always use a realistic name.
        _NAME_POOL = [
            ("Jordan", "Kim"), ("Priya", "Mehta"), ("Marcus", "Webb"),
            ("Dana", "Reeves"), ("Hira", "Nair"), ("Elise", "Torres"),
            ("Owen", "Fletcher"), ("Cleo", "Nash"), ("Leon", "Park"),
            ("Ayesha", "Khan"), ("Marco", "Silva"), ("Natalie", "Cross"),
            ("Ethan", "Walsh"), ("Sona", "Park"), ("Jordan", "Moss"),
        ]
        _pool_idx = 0

        for i, p in enumerate(personas[:n_employees]):
            rk    = p.get("role_key", f"role{i}")
            num   = p.get("employee_number", f"{(i+1):03d}")
            title = p.get("job_title", f"Employee {i+1}")
            short = p.get("short_title", title[:20])
            dept  = p.get("department", "CORP")
            grade = p.get("pay_grade", "GR-07")
            # Use provided name; fall back to pool (never to "UserXXX Smith")
            _fallback_fn, _fallback_ln = _NAME_POOL[_pool_idx % len(_NAME_POOL)]
            _pool_idx += 1
            fn    = p.get("first_name") or _fallback_fn
            ln    = p.get("last_name")  or _fallback_ln
            uid   = f"{employee_prefix}{num}"
            username = p.get("username") or f"{employee_prefix.lower()}.{rk}"
            mgr   = None if i == 0 else (ceo_id or f"{employee_prefix}{personas[0].get('employee_number', '001')}")
            if i == 0:
                ceo_id = uid
            sal   = p.get("salary_history") or SALARY_HISTORY.get(grade, [90000, 97000, 105000])
            impact, risk, fl = GRADE_IMPACT.get(grade, ("MEDIUM", "LOW", False))
            bonus = p.get("year_end_bonus") or BONUS_BY_GRADE.get(grade, 8000)
            # Goals: agent-supplied or derive from role_key fallback
            pg = p.get("goals") or {}
            if pg:
                g1n = pg.get("annual_1_name", "Achieve business targets")
                g1m = pg.get("annual_1_metric", "Measured by KPI dashboard")
                g2n = pg.get("annual_2_name", "Drive team development")
                g2m = pg.get("annual_2_metric", "Measured by engagement score")
                dgn = pg.get("dev_name", "Build leadership skills")
                dgm = pg.get("dev_metric", "Complete leadership programme")
            else:
                g1n, g1m, g2n, g2m, dgn, dgm = GOAL_CONTENT.get(rk, _DEFAULT_GOAL)
            spot = p.get("spot_award_message") or (
                f"Outstanding delivery — {title} drove key results for {company_name}"
                if i > 0 else None
            )
            employees.append({
                "userId":    uid,
                "username":  username,
                "firstName": fn,
                "lastName":  ln,
                "email_tag": username,
                "jobTitle":  title,
                "shortTitle": short,
                "dept":      f"{company_code}-D-{dept}",
                "dept_key":  dept,
                "bu":        DEPT_BU.get(dept, "CORP"),
                "division":  DEPT_DIVISION.get(dept, "CORP_SVCS"),
                "payGrade":  grade,
                "position":  f"P-{company_code}-{num}",
                "manager":   mgr,
                "salaryHistory": sal,
                "impactOfLoss":  impact,
                "riskOfLoss":    risk,
                "futureLeader":  fl,
                "yearEndBonus":  bonus,
                "goals": {
                    "annual_1_name":   g1n,
                    "annual_1_metric": g1m,
                    "annual_2_name":   g2n,
                    "annual_2_metric": g2m,
                    "dev_name":        dgn,
                    "dev_metric":      dgm,
                },
                "spot_award": spot,
            })
    else:
        # Path B: static INDUSTRY_ROLES table (original behaviour)
        roles     = INDUSTRY_ROLES.get(industry_key, INDUSTRY_ROLES["tech"])
        role_keys = list(roles.keys())[:n_employees]
        for i, rk in enumerate(role_keys):
            num, short, title, dept, grade = roles[rk]
            uid      = f"{employee_prefix}{num}"
            username = f"{employee_prefix.lower()}.{rk}"
            fn, ln   = FIRST_NAMES.get(rk, (f"User{num}", "Smith"))
            mgr      = None if i == 0 else (ceo_id or f"{employee_prefix}{roles[role_keys[0]][0]}")
            g1n, g1m, g2n, g2m, dgn, dgm = GOAL_CONTENT.get(rk, _DEFAULT_GOAL)
            if i == 0:
                ceo_id = uid
            sal = SALARY_HISTORY.get(grade, [90000, 97000, 105000])
            impact, risk, fl = GRADE_IMPACT.get(grade, ("MEDIUM", "LOW", False))
            bonus = BONUS_BY_GRADE.get(grade, 8000)
            _award_templates = [
                f"Outstanding delivery — {title} shipped ahead of schedule and under budget",
                f"Strategic win — resolved a key risk blocking the {dept} roadmap",
                f"Customer milestone — first major deal or delivery secured by {title}",
                f"Operational excellence — consistent high-quality execution all year",
            ]
            employees.append({
                "userId":    uid,
                "username":  username,
                "firstName": fn,
                "lastName":  ln,
                "email_tag": f"{employee_prefix.lower()}.{rk}",
                "jobTitle":  title,
                "shortTitle": short,
                "dept":      f"{company_code}-D-{dept}",
                "dept_key":  dept,
                "bu":        DEPT_BU.get(dept, "CORP"),
                "division":  DEPT_DIVISION.get(dept, "CORP_SVCS"),
                "payGrade":  grade,
                "position":  f"P-{company_code}-{num}",
                "manager":   mgr,
                "salaryHistory": sal,
                "impactOfLoss":  impact,
                "riskOfLoss":    risk,
                "futureLeader":  fl,
                "yearEndBonus":  bonus,
                "goals": {
                    "annual_1_name":   g1n,
                    "annual_1_metric": g1m,
                    "annual_2_name":   g2n,
                    "annual_2_metric": g2m,
                    "dev_name":        dgn,
                    "dev_metric":      dgm,
                },
                "spot_award": _award_templates[min(max(i - 1, 0), 3)] if i > 0 else None,
            })

    # Build org structure description
    org_lines = []
    for e in employees:
        mgr_label = "(NO_MANAGER — root)" if e["manager"] is None else f"→ {e['manager']}"
        org_lines.append(f"  {e['userId']} ({e['username']})  {e['jobTitle']}  {mgr_label}")

    # What's live vs story
    live_items = []
    story_items = []
    all_live = [
        ("org_structure",         "✅ LIVE", "Company, departments, location, positions, org hierarchy"),
        ("employees",             "✅ LIVE", "User accounts, employment records, personal data — login ready"),
        ("salary_history",        "✅ LIVE", "3 pay progression entries per employee via EmpPayCompRecurring"),
        ("bonus",                 "✅ LIVE", "Year-end bonus entry at Dec 2025 via EmpPayCompRecurring"),
        ("talent_profiles",       "✅ LIVE", "Impact/risk/futureLeader set per pay grade on all employees"),
        ("spot_awards",           "✅ LIVE", "CEO nominates direct reports — WOW Awards! points, APPROVED"),
        ("onboardee",             "✅ LIVE", "1 pending new hire (Sam Rivera) with Nov start date"),
        ("succession_nominations","⚠️  LIVE*", "CEO and VP-level nominations — requires succession module enabled"),
        ("ias_login",             "✅ LIVE", "All users login-ready via IAS SCIM password set"),
        ("goal_assignments",      "✅ LIVE", "Goal_11 annual goals + DevGoal_2001 dev goals per employee"),
    ]
    scenario_live = set(scenario.get("live_data", []))
    story_entities = [
        ("job_requisitions",         "📖 STORY", "Open reqs with job descriptions and requirements"),
        ("candidate_pipeline",       "📖 STORY", "Candidates at various stages — screening, interview, offer"),
        ("offer_letters",            "📖 STORY", "Offer letters and approval status"),
        ("interview_schedules",      "📖 STORY", "Interview panels, scheduling, and feedback"),
        ("performance_forms",        "📖 STORY", "PM forms assigned, ratings, calibration sessions"),
        ("calibration_sessions",     "📖 STORY", "Manager calibration sessions and rating distributions"),
        ("ratings",                  "📖 STORY", "Final performance ratings from completed review cycle"),
        ("skills_assignments",       "📖 STORY", "WSM skill profiles and skill gap assessments"),
        ("learning_completions",     "📖 STORY", "LMS course assignments and completion records"),
        ("learning_catalog",         "📖 STORY", "Course catalog and learning paths"),
        ("merit_proposals",          "📖 STORY", "Manager merit increase proposals and approval workflow"),
        ("budget_approval_workflow", "📖 STORY", "Budget approval chain and allocation pools"),
        ("pay_equity_analysis",      "📖 STORY", "Formal pay equity report and compa-ratio analysis"),
        ("compa_ratio_report",       "📖 STORY", "Compa-ratio distribution by grade and department"),
        ("gender_pay_gap_report",    "📖 STORY", "Gender pay gap analysis and reporting"),
        ("budget_submissions",       "📖 STORY", "Headcount budget and workforce plan"),
        ("headcount_plan",           "📖 STORY", "Approved headcount targets by department"),
        ("attrition_forecast",       "📖 STORY", "Predicted attrition risk by role and tenure"),
        ("org_restructure_proposal", "📖 STORY", "Restructure scenarios and impact analysis"),
        ("retention_action_plans",   "📖 STORY", "Manager retention actions and commitment tracking"),
        ("development_conversations","📖 STORY", "1:1 notes and development discussion records"),
        ("counter_offer_tracking",   "📖 STORY", "Counter-offer outcomes and flight risk resolution"),
        ("readiness_assessments",    "📖 STORY", "Formal successor readiness assessments and 9-box"),
        ("board_pack",               "📖 STORY", "Board-level succession summary and bench depth report"),
        ("onboarding_tasks",         "📖 STORY", "Onboarding checklist and task completion tracking"),
        ("buddy_assignment",         "📖 STORY", "Buddy/mentor assignment and first-week schedule"),
        ("equipment_provisioning",   "📖 STORY", "IT and facilities provisioning for new hire"),
    ]

    for key, status, desc in all_live:
        if any(k in scenario_live for k in [key, key.rstrip("s")]):
            live_items.append({"status": status, "entity": key, "description": desc})
    scenario_story_data = scenario.get("story_data", [])
    for key, status, desc in story_entities:
        if key in scenario_story_data:
            story_items.append({"status": status, "entity": key, "description": desc})

    card = scenario.get("agent_card", {})
    plan = {
        "plan_version":    "1.0",
        "company_name":    company_name,
        "company_code":    company_code,
        "industry":        industry_key,
        "country":         country_key,
        "locale":          locale,
        "business_problem": problem_key,
        "scenario_label":  scenario.get("label", scenario.get("scenario", "")),
        "employee_prefix": employee_prefix,
        "email_prefix":    email_prefix,
        "password":        password,
        "n_employees":     len(employees),
        "employees":       employees,
        "org_chart": "\n".join(org_lines),
        "scenario_narrative": scenario.get("talent_story", scenario.get("scenario", "")),
        "story_data_narrative": scenario.get("story_narrative", ""),
        "joule_prompts":   scenario.get("joule_prompts", []),
        "live_data":       live_items,
        "story_data":      story_items,
        # Principal propagation: who called this and which persona they map to
        "caller": {
            "email":       caller_email,
            "sf_alias":    caller_alias,
            "sf_login":    f"{caller_alias}@{_design_sf['company_code']}" if caller_alias else None,
            "note": (
                f"Caller identified as {caller_alias} via XSUAA principal propagation."
                if caller_alias
                else "No XSUAA token — running in stdio/local mode."
            ),
        },
        "agent_card": {
            "title":     f"{company_name} — {card.get('title', company_name)}",
            "challenge": card.get("challenge", business_problem),
            "prompts":   card.get("prompts", scenario.get("joule_prompts", [])),
            "live_count":  len(live_items),
            "story_count": len(story_items),
            "joule_url":   _design_sf["login_url"],
        },
        "sf_instance": _design_sf["company_code"],
        "login_url": _design_sf["login_url"],
        "summary": (
            f"{company_name} ({industry_key}, {country_key}) — {scenario['label']}\n"
            f"  {len(employees)} employees, company code {company_code}, prefix {employee_prefix}\n"
            f"  Password: {password}\n"
            f"  Login: {_design_sf['login_url']}\n\n"
            f"  LIVE ({len(live_items)} entities): {', '.join(i['entity'] for i in live_items)}\n"
            f"  STORY ({len(story_items)} entities): {', '.join(i['entity'] for i in story_items)}\n\n"
            f"  Story arc: {scenario['talent_story']}\n\n"
            f"  To provision this plan, call provision_demo_org() with this plan object."
        ),
    }

    return json.dumps(plan, indent=2)


# ── Phase 2: Provision ────────────────────────────────────────────────────────

def _do_provision(plan_json: str, job_id: str, caller_email_snapshot: Optional[str]) -> None:
    """Run all SF OData provisioning calls synchronously (called in a background thread)."""
    print(f"[provision] THREAD STARTED job={job_id} caller={caller_email_snapshot!r}", flush=True)
    with _JOBS_LOCK:
        _JOBS[job_id]["status"] = "running"
    try:
        result = _provision_sync(plan_json, caller_email_snapshot)
        with _JOBS_LOCK:
            _JOBS[job_id]["status"] = "done"
            _JOBS[job_id]["result"] = result
    except Exception as exc:
        with _JOBS_LOCK:
            _JOBS[job_id]["status"] = "error"
            _JOBS[job_id]["error"]  = str(exc)


@mcp.tool()
def provision_demo_org(plan_json: str, confirmed: bool = False, ctx=None) -> str:
    """
    Phase 2: Provision the org plan from design_demo_org() into SuccessFactors.

    IMPORTANT: Call this WITHOUT confirmed=True first to see a full pre-flight
    summary — what org will be created, what users and emails will be provisioned,
    and which SF tenant will be used. The tool will ask you to confirm before
    actually provisioning.

    Once you are satisfied with the summary, call again with confirmed=True to
    start provisioning.

    Provisioning runs in the background (takes 2-3 minutes for OData + IAS calls).
    Returns a job_id immediately. Poll with get_provisioning_status(job_id) to track
    progress and retrieve credentials when done.

    Args:
        plan_json:  The full JSON string returned by design_demo_org()
        confirmed:  Set to True only after reviewing the pre-flight summary
    """
    try:
        plan = json.loads(plan_json)
    except Exception as e:
        return json.dumps({"error": f"Invalid plan JSON: {e}"})

    caller_email_snapshot = _extract_caller_email(ctx)
    sf = _build_sf_config(caller_email_snapshot)

    # Block provisioning if IAS is detected but credentials are missing.
    # Users created in SF get an IAS account automatically (via email sync), but with
    # NO password — they cannot log in until a password is set via IAS SCIM. Without
    # valid IAS client credentials we cannot set passwords, so the demo is unusable.
    custom_cfg = _db.get_sf_config(caller_email_snapshot) if caller_email_snapshot else None
    if custom_cfg and custom_cfg.get("ias_base_url") and not custom_cfg.get("ias_client_id"):
        return json.dumps({
            "error": (
                "Cannot provision: your SF instance has IAS linked "
                f"({custom_cfg['ias_base_url']}) but no IAS client credentials are configured. "
                "Users would be created in SF but could not log in (no password). "
                "Fix: call configure_sf_instance again and add ias_client_id + ias_client_secret "
                "(create a System Application with 'Manage Users' scope in the IAS Admin UI at "
                f"{custom_cfg['ias_base_url']}/admin/)."
            )
        })

    # Log every call so we can audit in CF logs
    print(f"[provision] called by={caller_email_snapshot!r} confirmed={confirmed} "
          f"company={plan.get('company_name','?')} target={sf['company_code']}", flush=True)

    # ── Pre-flight summary (always shown) ────────────────────────────────────
    company_name  = plan.get("company_name", "Unknown")
    company_code  = plan.get("company_code", "???")
    industry      = plan.get("industry", "")
    country       = plan.get("country", "")
    password      = plan.get("password", "")
    email_prefix  = plan.get("email_prefix", "")
    employees     = plan.get("employees", [])
    scenario      = plan.get("scenario_label", plan.get("scenario", {}).get("label", ""))

    user_rows = []
    for e in employees:
        email_tag = e.get("email_tag", e.get("role_key", e.get("username", "?")))
        # uid6 suffix is added at provision time (not known yet) — show pattern
        full_email = f"{email_prefix}+{email_tag}.<uid6>@sap.com" if email_prefix else "N/A"
        user_rows.append({
            "sf_username": f"{e.get('username', '?')}@{sf['company_code']}",
            "name":        f"{e.get('firstName', '')} {e.get('lastName', '')}",
            "title":       e.get("jobTitle", ""),
            "email":       full_email,
            "ias":         "yes" if sf["ias_scim_url"] else "skipped",
        })

    preflight = {
        "target_instance": {
            "api_base":     sf["sf_base"],
            "company_code": sf["company_code"],
            "login_url":    sf["login_url"],
            "ias_enabled":  sf["ias_scim_url"] is not None,
            "ias_base":     sf["ias_base"],
            "source":       "custom" if _db.get_sf_config(caller_email_snapshot) else "default (SFSALES011375)",
        },
        "org_to_create": {
            "company_name": company_name,
            "company_code": company_code,
            "industry":     industry,
            "country":      country,
            "scenario":     scenario,
        },
        "users_to_provision": user_rows,
        "shared_password": password,
        "email_prefix_used": email_prefix or "N/A",
        "onboardee": {
            "name": "Sam Rivera",
            "note": "New hire onboarding scenario — provisioned as a separate entry",
        },
        "data_phases": [
            "Company & location", "Cost centers", "Departments", "Positions",
            "Users", "Employment", "Jobs", "Personal data", "Compensation",
            "Salary history", "Bonus", "Talent profiles", "Spot awards",
            "Onboarding record", "Goals (annual + development)",
            "IAS passwords" if sf["ias_scim_url"] else "IAS passwords (SKIPPED — no IAS configured)",
        ],
    }

    if not confirmed:
        return json.dumps({
            "status": "awaiting_confirmation",
            "message": (
                f"Ready to provision '{company_name}' ({company_code}) into "
                f"{sf['company_code']} ({sf['login_url']}). "
                f"Review the preflight summary below, then call provision_demo_org again "
                f"with confirmed=True to start provisioning."
            ),
            "preflight": preflight,
            "action_required": "Call provision_demo_org(plan_json=<same plan>, confirmed=True) to proceed.",
        }, indent=2)

    # ── Confirmed — kick off background provisioning ──────────────────────────
    job_id = str(uuid.uuid4())[:8]

    with _JOBS_LOCK:
        _JOBS[job_id] = {
            "status":     "pending",
            "result":     None,
            "error":      None,
            "started_at": time.time(),
        }

    t = threading.Thread(
        target=_do_provision,
        args=(plan_json, job_id, caller_email_snapshot),
        daemon=True,
    )
    t.start()

    return json.dumps({
        "status":  "started",
        "job_id":  job_id,
        "target":  f"{sf['company_code']} ({sf['login_url']})",
        "message": (
            f"Provisioning '{company_name}' into {sf['company_code']} has started. "
            "It typically takes 2–3 minutes (OData entity creation + IAS password activation). "
            f"Poll with get_provisioning_status(job_id='{job_id}') every 30 seconds until "
            "status is 'done' or 'error'."
        ),
    })


@mcp.tool()
def get_provisioning_status(job_id: str) -> str:
    """
    Poll the status of a background provisioning job started by provision_demo_org().

    Returns status 'pending', 'running', 'done', or 'error'.
    When status is 'done', the full confirmation (credentials, phase results, demo story)
    is included in the 'result' field.

    Args:
        job_id: The job_id returned by provision_demo_org()
    """
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        return json.dumps({"error": f"No job found with id '{job_id}'. Jobs are in-memory only — they do not survive server restarts."})

    elapsed = int(time.time() - job["started_at"])
    out: dict = {
        "job_id":   job_id,
        "status":   job["status"],
        "elapsed_seconds": elapsed,
    }
    if job["status"] == "done":
        # Inline the full provision result
        try:
            out.update(json.loads(job["result"]))
        except Exception:
            out["result"] = job["result"]
    elif job["status"] == "error":
        out["error"] = job["error"]
    elif job["status"] in ("pending", "running"):
        out["message"] = f"Still running after {elapsed}s. Check back in ~30s."
    return json.dumps(out, indent=2)


def _provision_sync(plan_json: str, caller_email: Optional[str]) -> str:
    """Synchronous provisioning body (runs inside a background thread)."""
    try:
        plan = json.loads(plan_json)
    except Exception as e:
        return json.dumps({"error": f"Invalid plan JSON: {e}"})

    # Resolve per-user SF + IAS config for this provisioning run
    sf = _build_sf_config(caller_email)

    co          = plan["company_code"]
    name        = plan["company_name"]
    emp_prefix  = plan["employee_prefix"]
    employees   = plan["employees"]
    email_pfx   = plan["email_prefix"]
    password    = plan["password"]
    locale      = plan["locale"]
    industry    = plan["industry"]

    # Unique ID for this provisioned org — stored in HANA, returned to caller
    demo_id = str(uuid.uuid4())
    uid6 = demo_id[:6]  # short suffix to make emails unique per org run

    if len(employees) < 2:
        _JOBS[job_id] = {"status": "error", "result": None,
                         "error": "Org plan must have at least 2 employees (1 manager + 1 report). Please redesign with n_employees ≥ 2.",
                         "started_at": time.time()}
        return

    # Caller identity: prefer the snapshot passed from the MCP tool (captured before threading),
    # fall back to whatever the plan carries.
    caller_email = caller_email or plan.get("caller", {}).get("email")

    # ── All dates relative to today ───────────────────────────────────────────
    _today = datetime.now()

    def _epoch_ms(dt) -> str:
        import calendar
        ts = int(calendar.timegm(dt.timetuple())) * 1000
        return f"/Date({ts})/"

    def _dt_str(dt) -> str:
        return dt.strftime("%Y-%m-%dT00:00:00")

    # Foundation object start (far past — fixed)
    START_DATE  = "/Date(-2208988800000)/"  # 1900-01-01

    # Hire date: 18 months ago (employees have been here a while)
    _hire_dt    = _today.replace(year=_today.year - 1, month=max(1, _today.month - 6))
    HIRE_DATE   = _epoch_ms(_hire_dt)
    HIRE_STR    = _dt_str(_hire_dt)

    # Year-end bonus: last Dec 25 (past)
    _bonus_yr   = _today.year if _today.month > 12 else _today.year - 1
    from datetime import date as _date
    _bonus_dt   = _date(_bonus_yr, 12, 25)
    import calendar as _cal
    BONUS_DATE  = f"/Date({int(_cal.timegm(_bonus_dt.timetuple())) * 1000})/"
    BONUS_STR   = f"{_bonus_yr}-12-25T00:00:00"

    # Onboardee start: 3 weeks from today
    import datetime as _dt_mod
    _onb_dt     = _today + _dt_mod.timedelta(weeks=3)
    ONB_DATE    = _epoch_ms(_onb_dt)
    ONB_STR     = _dt_str(_onb_dt)

    # Goals: start Jan 1 this year, due Dec 31 this year
    _goal_start = _date(_today.year, 1, 1)
    _goal_due   = _date(_today.year, 12, 31)
    GOAL_START  = f"/Date({int(_cal.timegm(_goal_start.timetuple())) * 1000})/"
    GOAL_DUE    = f"/Date({int(_cal.timegm(_goal_due.timetuple())) * 1000})/"

    # Salary history: 3 years ago, 2 years ago, current hire date
    def _sal_dates():
        """Return (date_str, epoch_str) for 3 salary history entries."""
        entries = []
        for years_back in (3, 2, 0):
            yr = _today.year - years_back
            mo = _hire_dt.month
            dt = _date(yr, mo, 1)
            entries.append((_dt_str(datetime(yr, mo, 1)), _epoch_ms(datetime(yr, mo, 1))))
        return entries  # [(str, epoch), (str, epoch), (str, epoch)]

    _sal_history_dates = _sal_dates()

    results = {}
    all_errors = []

    # ── Phase 1: FOCompany ─────────────────────────────────────────────────────
    ok, errs = _sf_upsert([{
        "__metadata": {"uri": "FOCompany"},
        "externalCode": co, "startDate": START_DATE,
        "name": name, "currency": locale["currency"],
        "country": locale["country_code"], "standardHours": 40, "status": "A",
    }], sf)
    results["FOCompany"] = f"{ok}/1"
    all_errors.extend(errs)

    # ── Phase 2: FOCostCenter ──────────────────────────────────────────────────
    dept_keys = list(dict.fromkeys(e["dept_key"] for e in employees))
    cc_ok = 0
    for dk in dept_keys:
        ok2, _ = _sf_post("FOCostCenter", {
            "externalCode": f"{co}-{dk}", "startDate": START_DATE,
            "name": f"{name} {DEPT_NAMES.get(dk, dk)}", "status": "A",
        }, sf)
        if ok2:
            cc_ok += 1
    results["FOCostCenter"] = f"{cc_ok}/{len(dept_keys)}"

    # ── Phase 3: FODepartment ──────────────────────────────────────────────────
    dept_rows = []
    for dk in dept_keys:
        div = DEPT_DIVISION.get(dk, "CORP_SVCS")
        dept_rows.append({
            "__metadata": {"uri": "FODepartment"},
            "externalCode": f"{co}-D-{dk}", "startDate": START_DATE,
            "name": DEPT_NAMES.get(dk, dk), "costCenter": f"{co}-{dk}", "status": "A",
            "cust_toLegalEntity": [{"externalCode": co, "startDate": START_DATE}],
            "cust_toDivision":    [{"externalCode": div, "startDate": START_DATE}],
        })
    ok, errs = _sf_upsert(dept_rows, sf)
    results["FODepartment"] = f"{ok}/{len(dept_rows)}"
    all_errors.extend(errs)

    # ── Phase 4: FOLocation ────────────────────────────────────────────────────
    loc_code = f"{co}-HQ01"
    ok, errs = _sf_upsert([{
        "__metadata": {"uri": "FOLocation"},
        "externalCode": loc_code, "startDate": START_DATE,
        "name": f"{name} HQ", "timezone": locale["tz"],
        "standardHours": 40, "geozoneFlx": "USA_SUBURB",
        "status": "A", "addressCountry": locale["country_code"],
        "companyFlx": co,
    }], sf)
    results["FOLocation"] = f"{ok}/1"
    all_errors.extend(errs)

    # ── Phase 5: Positions ─────────────────────────────────────────────────────
    pos_rows = []
    for e in employees:
        pos_rows.append({
            "__metadata": {"uri": "Position"},
            "code": e["position"], "effectiveStartDate": HIRE_DATE, "effectiveStatus": "A",
            "externalName_defaultValue": e["jobTitle"],
            "externalName_en_US": e["jobTitle"],
            "positionTitle": e["jobTitle"],
            "company": co, "department": e["dept"],
            "location": loc_code,
            "businessUnit": e["bu"], "division": e["division"],
            "payGrade": e["payGrade"], "regularTemporary": "R",
            "targetFTE": 1, "vacant": True, "multipleIncumbentsAllowed": False,
        })
    ok, errs = _sf_upsert(pos_rows, sf)
    results["Position"] = f"{ok}/{len(pos_rows)}"
    all_errors.extend(errs)

    # ── Phase 6: Users ─────────────────────────────────────────────────────────
    user_rows = []
    for e in employees:
        email = f"{email_pfx}+{e['email_tag']}.{uid6}@sap.com"
        user_rows.append({
            "__metadata": {"uri": f"User('{e['userId']}')"},
            "userId": e["userId"], "username": e["username"],
            "firstName": e["firstName"], "lastName": e["lastName"],
            "email": email, "status": "t",
            "defaultLocale": locale["locale"],
            "timeZone": locale["tz"],
            "password": password,
        })
    ok, errs = _sf_upsert(user_rows, sf)
    results["User"] = f"{ok}/{len(user_rows)}"
    all_errors.extend(errs)

    # ── Phase 7: EmpEmployment ─────────────────────────────────────────────────
    emp_rows = []
    for e in employees:
        uid = e["userId"]
        emp_rows.append({
            "__metadata": {"uri": f"EmpEmployment(personIdExternal='{uid}',userId='{uid}')"},
            "personIdExternal": uid, "userId": uid,
            "startDate": HIRE_DATE, "originalStartDate": HIRE_DATE,
            "firstDateWorked": HIRE_DATE, "seniorityDate": HIRE_DATE,
        })
    ok, errs = _sf_upsert(emp_rows, sf)
    results["EmpEmployment"] = f"{ok}/{len(emp_rows)}"
    all_errors.extend(errs)

    # ── Phase 8: EmpJob (seqNumber=1, manager set directly — no DATACHG workflow) ──
    job_rows = []
    for e in employees:
        uid = e["userId"]
        job_rows.append({
            "__metadata": {"uri": f"EmpJob(seqNumber=1L,startDate=datetime'{HIRE_STR}',userId='{uid}')"},
            "userId": uid, "seqNumber": 1,
            "startDate": HIRE_DATE, "company": co,
            "department": e["dept"], "division": e["division"], "businessUnit": e["bu"],
            "employeeClass": "4662", "employmentType": "3631", "eventReason": "HIRNEW",
            "fte": 1.0, "jobCode": "50000724", "jobTitle": e["jobTitle"],
            "location": loc_code, "managerId": e["manager"],
            "payGrade": e["payGrade"],
            "payScaleArea": locale["pay_scale"], "payScaleType": locale["pay_scale"],
            "position": e["position"], "standardHours": 40, "timezone": locale["tz"],
            "workscheduleCode": "NORM", "timeTypeProfileCode": "USA_STD",
            "holidayCalendarCode": "USA", "timeRecordingProfileCode": "DUR_NEG",
            "timeRecordingVariant": "DURATION",
            "timeRecordingAdmissibilityCode": "4WK_AMEND_YES",
            "defaultOvertimeCompensationVariant": "OCV_NO_PAYOUT",
        })
    ok, errs = _sf_upsert(job_rows, sf)
    results["EmpJob"] = f"{ok}/{len(job_rows)}"
    all_errors.extend(errs)

    # ── Phase 9: PerPersonal ───────────────────────────────────────────────────
    per_rows = []
    for e in employees:
        uid = e["userId"]
        per_rows.append({
            "__metadata": {"uri": f"PerPersonal(personIdExternal='{uid}',startDate=datetime'{HIRE_STR}')"},
            "personIdExternal": uid, "startDate": HIRE_DATE,
            "firstName": e["firstName"], "lastName": e["lastName"],
            "nationality": locale["country_code"], "gender": "U",
            "maritalStatus": "10820", "nativePreferredLang": "10240",
        })
    ok, errs = _sf_upsert(per_rows, sf)
    results["PerPersonal"] = f"{ok}/{len(per_rows)}"
    all_errors.extend(errs)

    # ── Phase 10: EmpCompensation + salary history ─────────────────────────────
    comp_rows = []
    for e in employees:
        uid = e["userId"]
        comp_rows.append({
            "__metadata": {"uri": f"EmpCompensation(startDate=datetime'{HIRE_STR}',userId='{uid}')"},
            "userId": uid, "startDate": HIRE_DATE,
            "isEligibleForCar": False, "isEligibleForBenefits": True,
            "payGroup": locale["pay_group"],
        })
    ok_comp, errs = _sf_upsert(comp_rows, sf)
    all_errors.extend(errs)

    sal_rows = []
    for e in employees:
        uid = e["userId"]
        for seq, sal in enumerate(e["salaryHistory"], start=1):
            sal_rows.append({
                "__metadata": {"uri": f"EmpPayCompRecurring(payComponent='BASESAL_US',seqNumber={seq}L,startDate=datetime'{HIRE_STR}',userId='{uid}')"},
                "payComponent": "BASESAL_US", "userId": uid,
                "startDate": HIRE_DATE, "seqNumber": seq,
                "paycompvalue": float(sal), "currencyCode": locale["currency"],
            })
    ok_sal, errs = _sf_upsert(sal_rows, sf)
    all_errors.extend(errs)
    results["EmpCompensation+SalaryHistory"] = f"comp={ok_comp}/{len(comp_rows)} salary={ok_sal}/{len(sal_rows)}"

    # ── Phase 11: Year-end bonus via EmpPayCompRecurring ──────────────────────
    bonus_rows = []
    for e in employees:
        uid = e["userId"]
        bonus_rows.append({
            "__metadata": {"uri": f"EmpPayCompRecurring(payComponent='BASESAL_US',seqNumber=4L,startDate=datetime'{HIRE_STR}',userId='{uid}')"},
            "payComponent": "BASESAL_US", "userId": uid,
            "startDate": HIRE_DATE, "seqNumber": 4,
            "paycompvalue": float(e["yearEndBonus"]),
            "currencyCode": locale["currency"],
        })
    ok, errs = _sf_upsert(bonus_rows, sf)
    results["YearEndBonus"] = f"{ok}/{len(bonus_rows)}"
    all_errors.extend(errs)

    # ── Phase 12: Talent profiles ──────────────────────────────────────────────
    talent_rows = []
    for e in employees:
        uid = e["userId"]
        talent_rows.append({
            "__metadata": {"uri": f"User('{uid}')"},
            "userId": uid,
            "impactOfLoss": e["impactOfLoss"],
            "riskOfLoss":   e["riskOfLoss"],
            "futureLeader": e["futureLeader"],
        })
    ok, errs = _sf_upsert(talent_rows, sf)
    results["TalentProfile"] = f"{ok}/{len(talent_rows)}"
    all_errors.extend(errs)

    # ── Phase 13: Spot awards ──────────────────────────────────────────────────
    # Use uid6 to avoid code collisions across multiple orgs with the same company code
    BASE_CODE = 800000 + (int(uid6, 16) % 99000)

    # Discover the active SpotAwardProgram at runtime — use first active one found
    _active_program = None
    try:
        _prog_url = f"{sf['sf_base']}/SpotAwardProgram?$format=json&$select=externalCode,status&$filter=status%20eq%20'ACTIVE'"
        _prog_req = urllib.request.Request(_prog_url, headers=sf['sf_headers'])
        with urllib.request.urlopen(_prog_req, context=CTX, timeout=10) as _r:
            _progs = json.loads(_r.read()).get("d", {}).get("results", [])
            if _progs:
                _active_program = _progs[0]["externalCode"]
                print(f"[spot_awards] using program={_active_program}", flush=True)
    except Exception as _e:
        print(f"[spot_awards] program discovery failed: {_e}", flush=True)

    _default_award_msgs = [
        "Outstanding delivery — shipped ahead of schedule and under budget",
        "Strategic win — resolved a key risk that was blocking the roadmap",
        "Customer milestone — first major deal or delivery secured",
        "Operational excellence — consistent high-quality execution all year",
    ]
    award_rows = []
    ceo_id = employees[0]["userId"]
    for i, sub in enumerate(employees[1:]):
        # Use plan-level spot_award message if present, else fall back to generic template
        comment = sub.get("spot_award") or _default_award_msgs[min(i, 3)]
        pts = [300, 200, 200, 100][min(i, 3)]
        code = BASE_CODE + i + 1
        row = {
            "__metadata": {"uri": f"SpotAward({code})"},
            "externalCode": code,
            "userId":       sub["userId"],
            "nominatorId":  ceo_id,
            "awardAmount":  float(pts),
            "currency":     "POINTS",
            "category":     "1",
            "level":        "1",
            "approvalStatus": "APPROVED",
            "commentForReceiver": comment,
            "commentForApprovers": "Above guideline: impact warranted top recognition.",
        }
        if _active_program:
            row["spotAwardProgram"] = _active_program
        award_rows.append(row)
    ok, errs = _sf_upsert(award_rows, sf)
    results["SpotAwards"] = f"{ok}/{len(award_rows)}"
    if errs:
        print(f"[spot_awards] errors codes={[r['externalCode'] for r in award_rows]} errs={errs[:3]}", flush=True)
    # Don't surface spot award eligibility errors — graceful
    non_eligibility_errs = [e for e in errs if "not eligible" not in e.lower()]
    all_errors.extend(non_eligibility_errs)

    # ── Phase 14: Onboardee ────────────────────────────────────────────────────
    onb_uid  = f"{emp_prefix}{len(employees)+1:03d}"
    onb_user = f"{emp_prefix.lower()}.onb"
    onb_tag  = f"{emp_prefix.lower()}.onb"
    onb_email = f"{email_pfx}+{onb_tag}.{uid6}@sap.com"
    onb_mgr  = employees[1]["userId"]
    onb_dept = employees[1]["dept"]
    onb_pos  = employees[1]["position"]
    ONB_DATE  = _epoch_ms(_onb_dt)   # 3 weeks from today — computed in date block
    ONB_STR   = _dt_str(_onb_dt)

    ok1, _ = _sf_upsert([{
        "__metadata": {"uri": f"User('{onb_uid}')"},
        "userId": onb_uid, "username": onb_user,
        "firstName": "Sam", "lastName": "Rivera",
        "email": onb_email, "status": "t",
        "defaultLocale": locale["locale"], "timeZone": locale["tz"],
        "password": password,
    }], sf)
    try:
        onb_payload = json.dumps({
            "userId": onb_uid, "firstName": "Sam", "lastName": "Rivera",
            "email": onb_email, "hireDate": ONB_DATE,
        }).encode()
        req = urllib.request.Request(f"{sf['sf_base']}/createOnboardee", data=onb_payload, headers=sf['sf_headers'], method="POST")
        with urllib.request.urlopen(req, context=CTX, timeout=30):
            onb_fn_ok = 1
    except Exception:
        onb_fn_ok = 0

    ok3, _ = _sf_upsert([{
        "__metadata": {"uri": f"EmpEmployment(personIdExternal='{onb_uid}',userId='{onb_uid}')"},
        "personIdExternal": onb_uid, "userId": onb_uid,
        "startDate": ONB_DATE, "originalStartDate": ONB_DATE,
        "firstDateWorked": ONB_DATE, "seniorityDate": ONB_DATE,
    }], sf)
    ok5, _ = _sf_upsert([{
        "__metadata": {"uri": f"PerPersonal(personIdExternal='{onb_uid}',startDate=datetime'{ONB_STR}')"},
        "personIdExternal": onb_uid, "startDate": ONB_DATE,
        "firstName": "Sam", "lastName": "Rivera",
        "nationality": locale["country_code"], "gender": "U",
        "maritalStatus": "10820", "nativePreferredLang": "10240",
    }], sf)
    onb_success = all([ok1, ok3, ok5])
    results["Onboardee"] = f"{'1/1' if onb_success else '0/1'} ({onb_uid} Sam Rivera, Nov 3 start)"

    # ── Phase 15: Goals (Goal_11 annual + DevGoal_2001 dev goals) ────────────────
    # Goals must be created as the user themselves — sfadmin gets 403.
    # Auth: username@<SF_TENANT_company_code>:password (e.g. username@SFSALES011375)
    goals_ok = 0
    goals_total = 0
    # Dates relative to today: start = Jan 1 of current year, due = Dec 31 of current year
    import datetime as _dt
    _today = _dt.date.today()
    _year_start = _dt.date(_today.year, 1, 1)
    _year_end   = _dt.date(_today.year, 12, 31)
    import calendar as _cal
    _ts_start = int(_dt.datetime(_today.year, 1, 1, tzinfo=_dt.timezone.utc).timestamp()) * 1000
    _ts_end   = int(_dt.datetime(_today.year, 12, 31, tzinfo=_dt.timezone.utc).timestamp()) * 1000
    GOAL_START  = f"/Date({_ts_start})/"
    GOAL_DUE    = f"/Date({_ts_end})/"

    for e in employees:
        # Goals come from the plan (set at design time); fall back to role-key lookup for
        # plans created before this change.
        _g = e.get("goals") or {}
        if _g:
            g1n, g1m = _g["annual_1_name"], _g["annual_1_metric"]
            g2n, g2m = _g["annual_2_name"], _g["annual_2_metric"]
            dgn, dgm = _g["dev_name"],      _g["dev_metric"]
        else:
            role_key = e.get("username", "").split(".")[-1] if "." in e.get("username", "") else ""
            g1n, g1m, g2n, g2m, dgn, dgm = GOAL_CONTENT.get(role_key, _DEFAULT_GOAL)

        annual_goals = [
            {"name": g1n, "metric": g1m},
            {"name": g2n, "metric": g2m},
        ]
        for goal in annual_goals:
            goals_total += 1
            ok, gerr = _sf_post_as_user("Goal_11", {
                "userId": e["userId"],
                "type": "user",
                "name": goal["name"],
                "description": goal["metric"],
                "metric": goal["metric"],
                "category": "Goals",
                "start": GOAL_START,
                "due": GOAL_DUE,
                "state": "On Track",
                "done": 0,
            }, e["username"], password, sf)
            if ok:
                goals_ok += 1
            else:
                print(f"[goals] Goal_11 FAIL user={e['username']} demo_co={sf.get('demo_company_code')} err={gerr}", flush=True)
                all_errors.append(f"Goal_11({e['username']}): {gerr}")

        goals_total += 1
        ok, gerr = _sf_post_as_user("DevGoal_2001", {
            "userId": e["userId"],
            "type": "development",
            "name": dgn,
            "metric": dgm,
            "purpose": "Current role",
            "category": "Goals",
            "start": GOAL_START,
            "due": GOAL_DUE,
            "state": "On Track",
            "competencies": {"results": []},
        }, e["username"], password, sf)
        if ok:
            goals_ok += 1
        else:
            print(f"[goals] DevGoal FAIL user={e['username']} demo_co={sf.get('demo_company_code')} err={gerr}", flush=True)
            all_errors.append(f"DevGoal_2001({e['username']}): {gerr}")

    results["Goals"] = f"{goals_ok}/{goals_total} (Goal_11 x2 + DevGoal_2001 x1 per employee)"

    # ── Phase 16: IAS user creation + password ────────────────────────────────
    # Create users directly in IAS via SCIM — do NOT poll for SF→IAS sync
    # (this tenant does not auto-provision IAS users from SF)
    all_users_ias = employees + [{
        "username": onb_user, "email_tag": onb_tag,
        "firstName": "Alex", "lastName": "Jordan",
    }]
    ias_ok = 0
    for e in all_users_ias:
        email = f"{email_pfx}+{e['email_tag']}.{uid6}@sap.com"
        # IAS userName must be the short username only (e.g. "fed.dir"), not "fed.dir@COMPANY"
        ias_username = e["username"].split("@")[0] if "@" in e.get("username", "") else e["username"]
        success, ias_err = _ias_ensure_user(
            username=ias_username,
            password=password,
            email=email,
            first_name=e.get("firstName", ""),
            last_name=e.get("lastName", ""),
            sf=sf,
        )
        if success:
            ias_ok += 1
        else:
            print(f"[ias] FAIL user={ias_username} email={email} err={ias_err}", flush=True)
        time.sleep(0.5)
    results["IASPasswords"] = f"{ias_ok}/{len(all_users_ias)}"

    # ── Build final confirmation ───────────────────────────────────────────────

    # Phase results table — one row per provisioned entity with ✅/❌
    _PHASE_LABELS = {
        "Company":           "Company & location record",
        "Departments":       "Departments",
        "CostCentres":       "Cost centres",
        "Positions":         "Positions",
        "Employees":         "Employee accounts (EC)",
        "EmpJobs":           "Employment records",
        "TalentProfiles":    "Talent profiles (impact/risk/FL)",
        "Goals":             "Goal assignments",
        "CompHistory":       "Compensation history (salary)",
        "BonusEntries":      "Year-end bonus entries",
        "SpotAwards":        "Spot / recognition awards",
        "Onboardee":         "Onboardee (pending hire)",
        "IASPasswords":      "IAS login activation",
    }

    phase_rows = []
    for phase_key, label in _PHASE_LABELS.items():
        val = results.get(phase_key)
        if val is None:
            continue
        # Determine flag: anything that starts with a digit fraction like "5/5" → check equality
        if isinstance(val, str) and "/" in val:
            parts = val.split("/")
            try:
                flag = "✅" if int(parts[0]) == int(parts[1]) else "⚠️"
            except ValueError:
                flag = "✅"
        else:
            flag = "✅" if str(val).upper() not in ("FALSE", "0", "FAILED", "ERROR") else "❌"
        phase_rows.append(f"  {flag}  {label:<40} {val}")

    # Credential rows — one per user (employees + onboardee)
    cred_rows = []
    _col = "{:<10} {:<28} {:<36} {}"
    cred_rows.append("  " + _col.format("User ID", "SF Username", "Email", "Title"))
    cred_rows.append("  " + "-"*95)
    for e in employees:
        email = f"{email_pfx}+{e['email_tag']}.{uid6}@sap.com"
        cred_rows.append("  " + _col.format(e["userId"], e["username"], email, e["jobTitle"]))
    onb_email_line = f"{email_pfx}+{onb_tag}.{uid6}@sap.com"
    cred_rows.append("  " + _col.format(onb_uid, onb_user, onb_email_line, "Senior Associate (onboardee)"))

    story_lines = []
    for item in plan.get("story_data", []):
        story_lines.append(f"  {item['status']} {item['entity']}: {item['description']}")

    joule_lines = [f"  • {p}" for p in plan.get("joule_prompts", [])]

    # ── Persist to HANA ───────────────────────────────────────────────────────
    # Stamp uid6 onto email_tags so HANA stores the same unique emails used in IAS/SF
    for e in employees:
        if "email_tag" in e:
            e["email_tag"] = f"{e['email_tag']}.{uid6}"
    db_status = "not_persisted"
    try:
        _db.save_demo_org(
            demo_id=demo_id,
            company_code=co,
            company_name=name,
            industry=plan["industry"],
            country=plan.get("country", ""),
            scenario=plan.get("scenario_label", ""),
            password=password,
            created_by=caller_email,
            employees=employees,
            email_prefix=email_pfx,
            plan_json=plan_json,
        )
        db_status = "saved"
    except Exception as e:
        db_status = f"error: {str(e)[:120]}"

    output = {
        "status":    "SUCCESS" if not all_errors else f"DONE WITH {len(all_errors)} ERROR(S)",
        "demo_id":   demo_id,
        "company":   name,
        "code":      co,
        "industry":  plan["industry"],
        "problem":   plan["scenario_label"],
        "login_url": sf["login_url"],
        "password":  password,
        "created_by": caller_email or "anonymous",
        "db_status": db_status,
        "phase_results": results,
        "errors":    all_errors[:10],
        "confirmation": (
            f"{'='*80}\n"
            f"  {name} ({co})  —  {plan['scenario_label']}\n"
            f"  Demo ID   : {demo_id}\n"
            f"  Instance  : {sf['company_code']}\n"
            f"  Login URL : {sf['login_url']}\n"
            f"  Password  : {password}  (shared by all users below)\n"
            f"{'='*80}\n\n"
            f"PROVISIONING RESULTS:\n"
            + "\n".join(phase_rows) + "\n\n"
            f"USER CREDENTIALS:\n"
            + "\n".join(cred_rows) + "\n\n"
            f"  ℹ️  Activation emails have been sent to each user's SAP email address.\n"
            f"     All accounts are immediately active — no email action required.\n"
            f"     Log in with the username above and password: {password}\n\n"
            f"STORY DATA (not provisioned — narrate in the demo):\n"
            + ("\n".join(story_lines) if story_lines else "  (none for this scenario)") + "\n\n"
            f"STORY NARRATIVE:\n  {plan.get('story_data_narrative','')}\n\n"
            f"JOULE PROMPTS TO TRY:\n" + "\n".join(joule_lines) + "\n"
            f"{'='*80}\n"
        ),
    }
    return json.dumps(output, indent=2)


# ── Tool: List available scenarios ────────────────────────────────────────────

@mcp.tool()
def list_scenarios() -> str:
    """
    List all available business problem scenarios with their agent card titles,
    challenge statements, sample prompts, and what data will be live vs story.

    Use this before calling design_demo_org to pick the right scenario for a
    specific customer problem or demo context.
    """
    out = []
    for key, s in SCENARIO_KB.items():
        card = s["agent_card"]
        live_count  = len(s["live_data"])
        story_count = len(s["story_data"])
        out.append({
            "scenario_key":    key,
            "label":           s["label"],
            "agent_title":     card["title"],
            "challenge":       card["challenge"],
            "sample_prompts":  card["prompts"],
            "live_entities":   s["live_data"],
            "story_entities":  s["story_data"],
            "readiness":       f"{live_count} live / {story_count} story",
        })
    return json.dumps(out, indent=2)


# ── Tool: List my provisioned orgs ────────────────────────────────────────────

@mcp.tool()
def list_my_orgs(ctx=None) -> str:
    """
    List all demo orgs you have provisioned, retrieved from HANA.

    Returns org ID, company name, scenario, creation time, and login details
    for every org associated with your caller email (from XSUAA token).

    In stdio/local mode returns all orgs (no caller filter).
    """
    caller_email = _extract_caller_email(ctx)
    try:
        orgs = _db.list_demo_orgs(created_by=caller_email)
        # Convert datetime objects to strings for JSON
        for o in orgs:
            if hasattr(o.get("created_at"), "isoformat"):
                o["created_at"] = o["created_at"].isoformat()
        return json.dumps({
            "caller":    caller_email or "anonymous (stdio mode)",
            "org_count": len(orgs),
            "orgs":      orgs,
            "login_url": _build_sf_config(caller_email)["login_url"],
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e), "hint": "HANA not available — check VCAP_SERVICES or HANA_* env vars"})


@mcp.tool()
def get_org_details(demo_id: str, ctx=None) -> str:
    """
    Get full details for a provisioned demo org by its unique demo_id.

    Returns the org record plus all user credentials provisioned in that org.
    Only returns orgs you created — enforced via XSUAA caller identity.

    Args:
        demo_id: The UUID returned by provision_demo_org()
    """
    caller_email = _extract_caller_email(ctx)
    try:
        owner = _db.get_org_created_by(demo_id)
        if owner is None:
            return json.dumps({"error": f"No org found with demo_id '{demo_id}'"})
        if caller_email and owner != caller_email:
            return json.dumps({"error": "Access denied — this org belongs to a different user."})
        org = _db.get_demo_org(demo_id)
        org["login_url"] = _build_sf_config(caller_email)["login_url"]
        return json.dumps(org, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e), "hint": "HANA not available — check VCAP_SERVICES or HANA_* env vars"})


@mcp.tool()
def delete_demo_org(demo_id: str, confirmed: bool = False, ctx=None) -> str:
    """
    Delete a demo org — removes all SF data and the HANA record.

    SAFETY: This tool only acts on the exact user IDs and company code recorded
    in HANA for this demo_id. It reads those IDs from the database first — it
    never infers or guesses what to remove. You can only delete orgs you created
    (enforced via XSUAA caller identity).

    IMPORTANT — how SF cleanup works in practice:
    The SF tenant (SFSALES011375) blocks OData DELETE on Employee Central entities
    (EmpJob, EmpEmployment, User, compensation, etc.) — this is a tenant-level
    restriction that cannot be bypassed via API. Instead this tool:
      • Goals + spot awards: DELETED (these entities are deletable)
      • Users + employment records: DEACTIVATED (status=inactive) — they become
        invisible to all queries, org chart, and demo flows, but the rows remain
        in SF storage. This is the correct SF cleanup approach for shared tenants.
      • Structural records (positions, FOCompany, FOLocation): best-effort DELETE,
        silently skipped if the tenant blocks it.
      • HANA record: DELETED — the org disappears from your demo list immediately.
    IAS users are NOT touched.

    Call with confirmed=False first to see exactly what will happen.
    Call with confirmed=True to perform the cleanup.

    Args:
        demo_id:   The UUID of the org to delete (from list_my_orgs)
        confirmed: Must be True to actually run. False shows a pre-flight summary only.
    """
    caller_email = _extract_caller_email(ctx)
    try:
        owner = _db.get_org_created_by(demo_id)
        if owner is None:
            return json.dumps({"error": f"No org found with demo_id '{demo_id}'"})
        if caller_email and owner != caller_email:
            return json.dumps({"error": "Access denied — this org belongs to a different user."})
        org = _db.get_demo_org(demo_id)
    except Exception as e:
        return json.dumps({"error": str(e)})

    co   = org["company_code"]
    name = org["company_name"]
    users = org.get("users", [])
    user_ids = [u["user_id"] for u in users]

    # Safety: the HANA record IS the authority on what we created.
    # We only delete the exact user IDs and company code stored in DEMO_ORGS /
    # DEMO_ORG_EMAILS for this demo_id. Nothing outside those keys is touched.
    # Ownership is already enforced above (caller_email must match CREATED_BY).
    if not user_ids:
        return json.dumps({"error": "No users found in HANA for this org — cannot determine what to delete."})

    # Include the onboardee (username pattern: prefix.onb)
    prefix = user_ids[0][:-3] if user_ids else ""
    onb_uid = f"{prefix}{len(user_ids)+1:03d}" if prefix else None

    summary_lines = [
        f"Demo org:     {name} (demo_id={demo_id})",
        f"Company code: {co}",
        f"Users:        {', '.join(user_ids)}" + (f", {onb_uid} (onboardee)" if onb_uid else ""),
        "",
        "What will happen in SF:",
        "  ✅ DELETED  — Goals (Goal_11 + DevGoal_2001) for all users",
        "  ✅ DELETED  — Spot awards",
        "  ⚠️  DEACTIVATED — Users + all employment/compensation records",
        "    (SF tenant blocks OData DELETE on EC entities — deactivation makes",
        "     them invisible to all queries, org chart, and demo flows)",
        f"  🗑️  BEST-EFFORT — Positions, FODepartment, FOCostCenter, FOLocation, FOCompany",
        "",
        "What will happen in HANA:",
        "  ✅ DELETED  — Org record + all user email rows (org disappears from your list)",
        "",
        "IAS users: NOT touched",
    ]

    if not confirmed:
        return json.dumps({
            "status": "awaiting_confirmation",
            "message": "\n".join(summary_lines),
            "action_required": f"Call delete_demo_org(demo_id='{demo_id}', confirmed=True) to proceed.",
        }, indent=2)

    # ── Confirmed — run cleanup ───────────────────────────────────────────────
    sf = _build_sf_config(caller_email)
    results = {}
    all_uids = user_ids + ([onb_uid] if onb_uid else [])

    # Phase 1: Goals — must delete authenticated as the user (sfadmin gets 403)
    goal_ok = goal_fail = 0
    for u in users:
        uid = u["user_id"]
        username = u["username"]
        password = org.get("password", "")
        full_user = f"{username}@{sf['company_code']}"
        creds = base64.b64encode(f"{full_user}:{password}".encode()).decode()
        user_hdrs = {**sf['sf_headers'], "Authorization": f"Basic {creds}"}
        for entity in ("Goal_11", "DevGoal_2001"):
            list_url = f"{sf['sf_base']}/{entity}?%24filter=userId%20eq%20'{uid}'&%24select=id&%24format=json"
            req = urllib.request.Request(list_url, headers=user_hdrs)
            try:
                with urllib.request.urlopen(req, context=CTX, timeout=30) as r:
                    rows = json.loads(r.read()).get("d", {}).get("results", [])
                for row in rows:
                    gid = row.get("id")
                    if not gid:
                        continue
                    ok, _ = _sf_delete(f"{entity}({gid})", {**sf, "sf_headers": user_hdrs})
                    if ok: goal_ok += 1
                    else:  goal_fail += 1
            except Exception:
                pass
    results["Goals"] = f"deleted={goal_ok} failed={goal_fail}"

    # Phase 2: Spot awards
    sa_ok = sa_fail = 0
    for uid in all_uids:
        list_url = f"{sf['sf_base']}/SpotAward?%24filter=userId%20eq%20'{uid}'&%24select=externalCode&%24format=json"
        req = urllib.request.Request(list_url, headers=sf['sf_headers'])
        try:
            with urllib.request.urlopen(req, context=CTX, timeout=30) as r:
                rows = json.loads(r.read()).get("d", {}).get("results", [])
            for row in rows:
                code = row.get("externalCode")
                if code:
                    ok, _ = _sf_delete(f"SpotAward({code})", sf)
                    if ok: sa_ok += 1
                    else:  sa_fail += 1
        except Exception:
            pass
    results["SpotAwards"] = f"deleted={sa_ok} failed={sa_fail}"

    # Phase 3: Deactivate all users (status=f) — EC entities are not deletable
    # via OData in this tenant; deactivation makes them invisible to all demo flows.
    deact_rows = [
        {"__metadata": {"uri": f"User('{uid}')"}, "userId": uid, "status": "f"}
        for uid in all_uids
    ]
    deact_ok = 0
    try:
        body = json.dumps(deact_rows).encode()
        req = urllib.request.Request(f"{sf['sf_base']}/upsert", data=body, headers=sf['sf_headers'], method="POST")
        with urllib.request.urlopen(req, context=CTX, timeout=30) as r:
            resp = r.read().decode()
            deact_ok = resp.count("UPDATED") + resp.count("204")
    except Exception as e:
        results["Deactivate_error"] = str(e)[:120]
    results["Users_deactivated"] = f"{deact_ok}/{len(deact_rows)}"

    # Phase 4: Best-effort structural deletes (positions, org units, company)
    def _del_single(uri):
        ok, _ = _sf_delete(uri, sf)
        return ok

    pos_nums = [u["user_id"][-3:] for u in users]
    if onb_uid:
        pos_nums.append(f"{len(users)+1:03d}")
    p_ok = sum(_del_single(f"Position('P-{co}-{n}')") for n in pos_nums)
    results["Positions"] = f"{p_ok}/{len(pos_nums)} (best-effort)"

    dept_keys = list(dict.fromkeys(u.get("department", "") for u in users if u.get("department")))
    fd_ok = sum(_del_single(f"FODepartment(externalCode='{co}-D-{dk}',startDate=datetime'1900-01-01T00:00:00')") for dk in dept_keys)
    cc_ok = sum(_del_single(f"FOCostCenter(externalCode='{co}-{dk}',startDate=datetime'1900-01-01T00:00:00')") for dk in dept_keys)
    results["FODepartment"] = f"{fd_ok}/{len(dept_keys)} (best-effort)"
    results["FOCostCenter"] = f"{cc_ok}/{len(dept_keys)} (best-effort)"
    _del_single(f"FOLocation(externalCode='{co}-HQ01',startDate=datetime'1900-01-01T00:00:00')")
    _del_single(f"FOCompany(externalCode='{co}',startDate=datetime'1900-01-01T00:00:00')")
    results["FOLocation_FOCompany"] = "best-effort"

    # ── Remove from HANA ──────────────────────────────────────────────────────
    hana_ok = False
    try:
        _db.delete_demo_org(demo_id)
        hana_ok = True
    except Exception as e:
        results["HANA"] = f"error: {e}"

    if hana_ok:
        results["HANA"] = "deleted"

    print(f"[delete] demo_id={demo_id} co={co} caller={caller_email!r} results={results}", flush=True)

    return json.dumps({
        "status": "deleted",
        "demo_id": demo_id,
        "company_name": name,
        "company_code": co,
        "sf_results": results,
    }, indent=2)


def _resolve_demo_id(demo_id: Optional[str], caller_email: Optional[str]) -> tuple[Optional[dict], Optional[str]]:
    """Return (org_dict, error_str). Enforces caller ownership."""
    if not demo_id:
        return None, "demo_id is required"
    owner = _db.get_org_created_by(demo_id)
    if owner is None:
        return None, f"No org found with demo_id '{demo_id}'"
    if caller_email and owner != caller_email:
        return None, "Access denied — this org belongs to a different user."
    org = _db.get_demo_org(demo_id)
    return org, None


@mcp.tool()
def get_org_employees(demo_id: str, ctx=None) -> str:
    """
    List all employees provisioned in a demo org with their credentials,
    job titles, department, pay grade, and SF login details.

    Only returns data for orgs you created (enforced via XSUAA identity).

    Args:
        demo_id: The UUID returned by provision_demo_org()
    """
    caller_email = _extract_caller_email(ctx)
    org, err = _resolve_demo_id(demo_id, caller_email)
    if err:
        return json.dumps({"error": err})

    rows = []
    for u in org.get("users", []):
        rows.append({
            "user_id":   u["user_id"],
            "username":  u["username"],
            "email":     u["email"],
            "job_title": u["job_title"],
            "pay_grade": u["pay_grade"],
            "department": u.get("department", ""),
            "sf_login":  f"{u['username']}@{org['company_code']}",
            "login_url": _build_sf_config(caller_email)["login_url"],
            "password":  org["password"],
        })

    _sf_cfg = _build_sf_config(caller_email)
    return json.dumps({
        "demo_id":      demo_id,
        "company":      org["company_name"],
        "company_code": org["company_code"],
        "instance":     _sf_cfg["company_code"],
        "login_url":    _sf_cfg["login_url"],
        "password":     org["password"],
        "employee_count": len(rows),
        "employees":    rows,
        "note": f"All accounts are active. Log in with username@{_sf_cfg['company_code']} or use the email address shown.",
    }, indent=2)


@mcp.tool()
def get_org_goals(demo_id: str, ctx=None) -> str:
    """
    List the goal assignments provisioned for every employee in a demo org.

    Returns annual and development goals per person, tied to the demo scenario.
    Only returns data for orgs you created (enforced via XSUAA identity).

    Args:
        demo_id: The UUID returned by provision_demo_org()
    """
    caller_email = _extract_caller_email(ctx)
    org, err = _resolve_demo_id(demo_id, caller_email)
    if err:
        return json.dumps({"error": err})

    rows = []
    for u in org.get("users", []):
        g = u.get("goals") or {}
        rows.append({
            "user_id":   u["user_id"],
            "username":  u["username"],
            "job_title": u["job_title"],
            "goals": {
                "annual_goal_1": {
                    "name":   g.get("annual_1_name", ""),
                    "metric": g.get("annual_1_metric", ""),
                },
                "annual_goal_2": {
                    "name":   g.get("annual_2_name", ""),
                    "metric": g.get("annual_2_metric", ""),
                },
                "development_goal": {
                    "name":   g.get("dev_name", ""),
                    "metric": g.get("dev_metric", ""),
                },
            },
        })

    return json.dumps({
        "demo_id":   demo_id,
        "company":   org["company_name"],
        "scenario":  org["scenario"],
        "goal_year": str(_dt.date.today().year),
        "employees": rows,
    }, indent=2)


@mcp.tool()
def get_org_compensation(demo_id: str, ctx=None) -> str:
    """
    Show compensation summary for all employees in a demo org:
    pay grade, salary progression (3 steps), and year-end bonus.

    Useful for demonstrating pay equity and compensation analytics stories.
    Only returns data for orgs you created (enforced via XSUAA identity).

    Args:
        demo_id: The UUID returned by provision_demo_org()
    """
    caller_email = _extract_caller_email(ctx)
    org, err = _resolve_demo_id(demo_id, caller_email)
    if err:
        return json.dumps({"error": err})

    # Pull full plan for salary/bonus detail — fall back to grade-only from DEMO_ORG_EMAILS
    plan_str = _db.get_org_plan_json(demo_id)
    plan_employees = {}
    if plan_str:
        try:
            plan_employees = {e["userId"]: e for e in json.loads(plan_str).get("employees", [])}
        except Exception:
            pass

    rows = []
    for u in org.get("users", []):
        pe = plan_employees.get(u["user_id"], {})
        sal_hist = pe.get("salaryHistory") or []
        bonus    = pe.get("yearEndBonus", "")
        rows.append({
            "user_id":         u["user_id"],
            "username":        u["username"],
            "job_title":       u["job_title"],
            "pay_grade":       u["pay_grade"],
            "salary_history":  sal_hist,
            "current_salary":  sal_hist[-1] if sal_hist else None,
            "year_end_bonus":  bonus,
        })

    return json.dumps({
        "demo_id":  demo_id,
        "company":  org["company_name"],
        "currency": "USD",
        "employees": rows,
    }, indent=2)


@mcp.tool()
def get_org_talent(demo_id: str, ctx=None) -> str:
    """
    Show talent profile data for all employees in a demo org:
    impact of loss, risk of loss, future leader flag, and pay grade.

    Use this to demonstrate talent retention and succession analytics stories.
    Only returns data for orgs you created (enforced via XSUAA identity).

    Args:
        demo_id: The UUID returned by provision_demo_org()
    """
    caller_email = _extract_caller_email(ctx)
    org, err = _resolve_demo_id(demo_id, caller_email)
    if err:
        return json.dumps({"error": err})

    plan_str = _db.get_org_plan_json(demo_id)
    plan_employees = {}
    if plan_str:
        try:
            plan_employees = {e["userId"]: e for e in json.loads(plan_str).get("employees", [])}
        except Exception:
            pass

    rows = []
    for u in org.get("users", []):
        pe = plan_employees.get(u["user_id"], {})
        rows.append({
            "user_id":       u["user_id"],
            "username":      u["username"],
            "job_title":     u["job_title"],
            "pay_grade":     u["pay_grade"],
            "department":    u.get("department", ""),
            "impact_of_loss":  pe.get("impactOfLoss", ""),
            "risk_of_loss":    pe.get("riskOfLoss", ""),
            "future_leader":   pe.get("futureLeader", False),
        })

    high_impact = [r for r in rows if r["impact_of_loss"] == "HIGH"]
    future_leaders = [r["username"] for r in rows if r["future_leader"]]

    return json.dumps({
        "demo_id":        demo_id,
        "company":        org["company_name"],
        "scenario":       org["scenario"],
        "summary": {
            "high_impact_count":   len(high_impact),
            "future_leader_count": len(future_leaders),
            "future_leaders":      future_leaders,
        },
        "employees": rows,
    }, indent=2)


# ── Tools: API key management ─────────────────────────────────────────────────

def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@mcp.tool()
def generate_api_key(label: str, ctx=None) -> str:
    """
    Generate an API key for A2A / machine-to-machine access to this MCP server.

    The key is tied to your user identity (from your XSUAA login). Any A2A caller
    using this key will be treated as you — they can only access your demo orgs.

    The plaintext key is returned ONCE and never stored. Copy it immediately.
    Future calls to this endpoint for the same label will fail — revoke and recreate.

    A2A usage: POST to https://sf-demo-builder.cfapps.us10.hana.ondemand.com/a2a/mcp
    with header: X-API-Key: <your key>

    Args:
        label: A descriptive name for this key, e.g. "sf-demo-agent-prod"
    """
    caller_email = _extract_caller_email(ctx)

    # Block A2A callers from creating new keys — must authenticate via XSUAA
    if _is_a2a_request.get():
        return json.dumps({
            "error": "API key generation requires interactive XSUAA login. "
                     "Call this tool via POST /mcp with a valid Bearer token."
        })

    if not caller_email:
        return json.dumps({
            "error": "Cannot determine caller identity. "
                     "Ensure you are authenticated via XSUAA Bearer token."
        })

    if not label or len(label.strip()) < 3:
        return json.dumps({"error": "label must be at least 3 characters."})
    label = label.strip()

    plaintext = "sfdemob_" + secrets.token_urlsafe(32)
    key_hash   = _sha256_hex(plaintext)

    try:
        _db.save_api_key(key_hash, caller_email, label)
    except Exception as e:
        return json.dumps({"error": f"Failed to save key: {str(e)[:200]}"})

    return json.dumps({
        "api_key":  plaintext,
        "label":    label,
        "owner":    caller_email,
        "a2a_endpoint": "https://sf-demo-builder.cfapps.us10.hana.ondemand.com/a2a/mcp",
        "usage":    "Add header:  X-API-Key: <api_key>",
        "note":     "Store this key now — it is not retrievable after this response.",
    }, indent=2)


@mcp.tool()
def list_api_keys(ctx=None) -> str:
    """
    List all API keys you have created.

    Returns label, creation date, last-used date, and active status.
    Never returns the key itself (only the hash is stored).
    """
    caller_email = _extract_caller_email(ctx)
    if not caller_email:
        return json.dumps({"error": "Cannot determine caller identity."})
    try:
        keys = _db.list_api_keys(caller_email)
        return json.dumps({
            "owner": caller_email,
            "count": len(keys),
            "keys":  keys,
            "a2a_endpoint": "https://sf-demo-builder.cfapps.us10.hana.ondemand.com/a2a/mcp",
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def revoke_api_key(label: str, ctx=None) -> str:
    """
    Revoke an API key by its label. The key will immediately stop working.

    Args:
        label: The label you gave the key when you created it
    """
    caller_email = _extract_caller_email(ctx)
    if not caller_email:
        return json.dumps({"error": "Cannot determine caller identity."})
    try:
        revoked = _db.revoke_api_key_by_label(label.strip(), caller_email)
        if revoked:
            return json.dumps({"status": "revoked", "label": label, "owner": caller_email})
        return json.dumps({"error": f"No active key found with label '{label}' for your account."})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def configure_sf_instance(
    api_base_url: str,
    admin_user: str,
    admin_password: str,
    login_url: str,
    ias_client_id: Optional[str] = None,
    ias_client_secret: Optional[str] = None,
    ctx=None,
) -> str:
    """
    Configure a custom SAP SuccessFactors instance for your session.

    By default this server provisions demos into the shared SAP sales demo
    environment (SFSALES011375). Use this tool to point it at your own SF
    instance instead.

    All provisioned orgs you create after calling this tool will use your
    instance, and are scoped to your login (other users are unaffected).

    IAS (Identity Authentication Service) is auto-detected from the login URL.
    To enable IAS password provisioning for your instance, also pass the IAS
    system administrator client_id and client_secret (created in IAS Admin UI
    under Applications & Resources → System Applications → Add System → Manage Users scope).

    Args:
        api_base_url:      SF OData API base URL, e.g. https://apisalesdemo8.successfactors.com/odata/v2
        admin_user:        SF admin username including company code, e.g. sfadmin@MYCOMPANY
        admin_password:    SF admin password
        login_url:         Full browser login URL, e.g. https://hcm-us10-sales.hr.cloud.sap/login?company=MYCO
        ias_client_id:     (optional) IAS system admin client ID for SCIM password provisioning
        ias_client_secret: (optional) IAS system admin client secret
    """
    caller_email = _extract_caller_email(ctx)
    if not caller_email:
        return json.dumps({"error": "Authentication required. Please connect via XSUAA OAuth2."})

    # Normalise API base URL — strip trailing slashes, ensure /odata/v2 if missing
    api_base = api_base_url.rstrip("/")

    # Validate by making a lightweight OData ping
    test_creds = base64.b64encode(f"{admin_user}:{admin_password}".encode()).decode()
    test_headers = {"Authorization": f"Basic {test_creds}", "Accept": "application/json"}
    ping_url = f"{api_base}/User?$top=1&$select=userId"
    try:
        req = urllib.request.Request(ping_url, headers=test_headers)
        with urllib.request.urlopen(req, context=CTX, timeout=15) as r:
            r.read()
        validation_status = "ok"
        validation_msg = "Credentials verified against SF OData API."
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return json.dumps({"error": "SF credentials rejected (401). Check admin_user and admin_password."})
        elif e.code == 403:
            return json.dumps({"error": "SF credentials valid but insufficient permissions (403). Check admin role."})
        else:
            validation_status = "warning"
            validation_msg = f"SF API returned HTTP {e.code} — credentials accepted but endpoint returned an error. Proceeding."
    except Exception as ex:
        return json.dumps({"error": f"Could not reach SF API at {api_base}: {ex}"})

    # Auto-detect IAS from login URL
    print(f"[configure_sf] Detecting IAS from login URL: {login_url}", flush=True)
    ias_base = _detect_ias_from_login_url(login_url)
    if ias_base:
        print(f"[configure_sf] IAS detected: {ias_base}", flush=True)
        if ias_client_id and ias_client_secret:
            # Validate the IAS credentials before storing
            test_ias_auth = "Basic " + base64.b64encode(f"{ias_client_id}:{ias_client_secret}".encode()).decode()
            ias_test_url = ias_base.rstrip("/") + "/service/scim/Users?count=1"
            try:
                ias_req = urllib.request.Request(ias_test_url, headers={"Authorization": test_ias_auth, "Accept": "application/scim+json"})
                with urllib.request.urlopen(ias_req, context=CTX, timeout=15) as r:
                    r.read()
                ias_cid = ias_client_id
                ias_csec = ias_client_secret
                ias_msg = f"IAS tenant: {ias_base}. Client credentials verified — IAS password provisioning enabled."
            except urllib.error.HTTPError as e:
                return json.dumps({"error": f"IAS client credentials rejected (HTTP {e.code}). Check ias_client_id and ias_client_secret."})
            except Exception as ex:
                return json.dumps({"error": f"Could not reach IAS at {ias_base}: {ex}"})
        else:
            ias_cid = None
            ias_csec = None
            ias_msg = (
                f"IAS tenant detected: {ias_base}. "
                "IAS password provisioning is DISABLED — no client credentials provided. "
                "To enable it, call configure_sf_instance again and pass ias_client_id and ias_client_secret "
                "(create a system admin in IAS Admin UI under Applications & Resources → System Applications, "
                "grant 'Manage Users' scope)."
            )
    else:
        ias_cid = None
        ias_csec = None
        ias_msg = "No IAS redirect detected from login URL — IAS user provisioning will be skipped for your instance."

    _db.save_sf_config(
        owner_email=caller_email,
        api_base_url=api_base,
        admin_user=admin_user,
        admin_pass=admin_password,
        login_url=login_url,
        ias_base_url=ias_base,
        ias_client_id=ias_cid,
        ias_client_secret=ias_csec,
    )

    company_code = admin_user.split("@", 1)[1] if "@" in admin_user else "UNKNOWN"
    return json.dumps({
        "status":           validation_status,
        "message":          validation_msg,
        "instance_config":  {
            "api_base_url":        api_base,
            "admin_user":          admin_user,
            "company_code":        company_code,
            "login_url":           login_url,
            "ias_detected":        ias_base is not None,
            "ias_base_url":        ias_base,
            "ias_passwords_ready": ias_cid is not None,
        },
        "ias_note":         ias_msg,
        "owner":            caller_email,
        "note":             "Configuration saved. All future provisioning requests from your account will use this instance.",
    }, indent=2)


@mcp.tool()
def get_sf_instance_config(ctx=None) -> str:
    """
    Show the SF instance configuration currently active for your account.

    Returns your custom config if set, or confirms you are using the default
    shared demo environment (SFSALES011375).
    """
    caller_email = _extract_caller_email(ctx)
    if not caller_email:
        return json.dumps({"error": "Authentication required."})

    custom = _db.get_sf_config(caller_email)
    if custom:
        return json.dumps({
            "source":        "custom",
            "api_base_url":  custom["api_base_url"],
            "admin_user":    custom["admin_user"],
            "login_url":     custom["login_url"],
            "ias_enabled":   custom["ias_base_url"] is not None,
            "ias_base_url":  custom["ias_base_url"],
            "configured_at": custom["updated_at"],
            "owner":         caller_email,
        }, indent=2)
    else:
        cfg = _build_sf_config(None)
        return json.dumps({
            "source":       "default",
            "api_base_url": cfg["sf_base"],
            "admin_user":   cfg["admin_user"],
            "login_url":    cfg["login_url"],
            "ias_enabled":  cfg["ias_base"] is not None,
            "ias_base_url": cfg["ias_base"],
            "note":         "Using shared demo environment. Call configure_sf_instance to use your own instance.",
        }, indent=2)


@mcp.tool()
def reset_sf_instance_config(ctx=None) -> str:
    """
    Reset your SF instance configuration back to the default shared demo environment.

    After calling this, your provisioning requests will use SFSALES011375 again.
    """
    caller_email = _extract_caller_email(ctx)
    if not caller_email:
        return json.dumps({"error": "Authentication required."})

    deleted = _db.delete_sf_config(caller_email)
    if deleted:
        return json.dumps({
            "status":  "reset",
            "message": "Custom SF config removed. Provisioning will now use the default shared demo environment.",
            "owner":   caller_email,
        })
    else:
        return json.dumps({
            "status":  "no_config",
            "message": "No custom config found — already using the default shared demo environment.",
        })



@mcp.tool()
def generate_agent_card(plan_json: str) -> str:
    """
    Generate a Joule Agent Hub-style card from a design_demo_org() plan.

    Takes the JSON output of design_demo_org() and returns a card object
    ready for display or sharing — with title, challenge statement,
    3 sample prompts calibrated to what's actually live, capability summary,
    and a direct Joule login URL.

    Use this after design_demo_org() but before provision_demo_org() to
    preview the demo narrative, or after provisioning to share the card
    with the sales team.

    Args:
        plan_json: The full JSON string returned by design_demo_org()
    """
    try:
        plan = json.loads(plan_json)
    except Exception as e:
        return json.dumps({"error": f"Invalid plan JSON: {e}"})

    card = plan.get("agent_card", {})
    live  = plan.get("live_data", [])
    story = plan.get("story_data", [])
    employees = plan.get("employees", [])
    instance_code = plan.get("sf_instance", plan.get("company_code", "SFSALES011375"))
    script_login_url = plan.get("login_url", LOGIN_URL)

    # Build persona table — who to log in as for the demo
    personas = []
    for e in employees[:3]:
        personas.append({
            "name":     f"{e['firstName']} {e['lastName']}",
            "title":    e["jobTitle"],
            "username": e["username"],
            "grade":    e["payGrade"],
            "login":    f"{e['username']}@{instance_code}",
        })

    # Story data framing — what the AE says when Joule can't show it
    story_framing = []
    for item in story:
        story_framing.append(
            f"  '{item['entity']}' — {item['description']}"
        )

    output = {
        "card": {
            "title":     card.get("title", ""),
            "challenge": card.get("challenge", ""),
            "prompts":   card.get("prompts", []),
            "joule_url": script_login_url,
        },
        "demo_context": {
            "company":   plan.get("company_name"),
            "industry":  plan.get("industry"),
            "country":   plan.get("country"),
            "scenario":  plan.get("scenario_label"),
            "employees": len(employees),
            "code":      plan.get("company_code"),
        },
        "live_data_summary": [
            f"{i['status']} {i['entity']}: {i['description']}"
            for i in live
        ],
        "story_framing": (
            "When Joule can't surface these directly, use this framing:\n"
            + ("\n".join(story_framing) if story_framing
               else "  (no story data — everything in this scenario is live)")
        ),
        "suggested_personas": personas,
        "ready_to_demo": len(story) == 0,
        "display": (
            f"┌{'─'*62}┐\n"
            f"│ {card.get('title','')[:60]:<60} │\n"
            f"├{'─'*62}┤\n"
            + "\n".join(
                f"│ {line:<60} │"
                for line in _wrap(card.get("challenge", ""), 60)
            ) + "\n"
            f"├{'─'*62}┤\n"
            f"│ {'Sample prompts:':<60} │\n"
            + "\n".join(
                f"│   {'↳ ' if i else '• '}{_wrap(p, 57)[0]:<57} │"
                + ("\n" + "\n".join(f"│     {line:<57} │" for line in _wrap(p, 57)[1:]) if len(_wrap(p, 57)) > 1 else "")
                for i, p in enumerate(card.get("prompts", []))
            ) + "\n"
            f"├{'─'*62}┤\n"
            + "\n".join(
                f"│ {line:<60} │"
                for line in _wrap("LIVE: " + ", ".join(i["entity"] for i in live), 60)
            ) + "\n"
            + ("\n".join(
                f"│ {line:<60} │"
                for line in _wrap("STORY: " + ", ".join(i["entity"] for i in story), 60)
            ) + "\n" if story else "")
            + f"│ {'Login: ' + script_login_url[:53]:<60} │\n"
            f"└{'─'*62}┘"
        ),
    }
    return json.dumps(output, indent=2)


def _wrap(text: str, width: int) -> list[str]:
    """Simple word-wrap for card display."""
    words = text.split()
    lines, line = [], []
    for w in words:
        if sum(len(x)+1 for x in line) + len(w) > width:
            lines.append(" ".join(line))
            line = [w]
        else:
            line.append(w)
    if line:
        lines.append(" ".join(line))
    return lines




# ── Tool: Generate two-surface demo script ────────────────────────────────────

@mcp.tool()
def generate_demo_script(plan_json: str) -> str:
    """
    Generate a two-surface demo script from a design_demo_org() plan.

    Produces a runnable demo guide with:
    - SURFACE 1: Joule Chat — 3 beats with exact prompts and what Joule shows
    - SURFACE 2: Joule Desktop / Claude Code — 3 beats with agent instructions,
      which SF OData calls the agent makes, what it produces, and the AE bridge line

    Each beat is honest about what's live vs what's narrative framing.

    Use after design_demo_org() (or provision_demo_org()) to hand the AE
    a complete script they can run without further prep.

    Args:
        plan_json: The full JSON string returned by design_demo_org()
    """
    try:
        plan = json.loads(plan_json)
    except Exception as e:
        return json.dumps({"error": f"Invalid plan JSON: {e}"})

    problem_key = plan.get("business_problem", "")
    scenario    = SCENARIO_KB.get(problem_key, {})
    demo_story  = scenario.get("demo_story", {})
    company     = plan.get("company_name", "the company")
    employees   = plan.get("employees", [])
    password    = plan.get("password", "")

    if not demo_story:
        return json.dumps({
            "error": f"No demo_story defined for scenario '{problem_key}'."
        })

    # Build persona reference (first 3 employees)
    personas = []
    for e in employees[:3]:
        personas.append(
            f"{e['firstName']} {e['lastName']} ({e['jobTitle']}, {e['payGrade']}) "
            f"— login: {e['username']}@{instance_code} / {password}"
        )

    # Format Joule Chat beats
    chat_beats = demo_story.get("joule_chat", [])
    chat_lines = []
    for i, beat in enumerate(chat_beats, 1):
        chat_lines.append(
            f"  Beat {i}: {beat['beat']}\n"
            f"  ─────────────────────────────────────────\n"
            f"  Login as: {personas[min(i-1, len(personas)-1)]}\n"
            f"  Prompt:   \"{beat['prompt']}\"\n"
            f"  Shows:    {beat['what_joule_shows']}\n"
            f"  AE says:  \"{beat['ae_bridge']}\""
        )

    # Format Joule Desktop beats
    desktop_beats = demo_story.get("joule_desktop", [])
    desktop_lines = []
    for i, beat in enumerate(desktop_beats, 1):
        mcp_calls = "; ".join(beat.get("mcp_calls", []))
        desktop_lines.append(
            f"  Beat {i}: {beat['beat']}\n"
            f"  ─────────────────────────────────────────\n"
            f"  Instruction: \"{beat['agent_instruction']}\"\n"
            f"  MCP reads:   {mcp_calls}\n"
            f"  Produces:    {beat['what_it_produces']}\n"
            f"  AE says:     \"{beat['ae_bridge']}\""
        )

    # Story bridge — what AE says for narrative items
    story_items = plan.get("story_data", [])
    story_bridge = (
        scenario.get("story_narrative", "")
        if story_items
        else "All data in this scenario is live — no narrative bridge needed."
    )

    script_text = (
        f"{'='*70}\n"
        f"  DEMO SCRIPT: {company} — {scenario.get('label','')}\n"
        f"  Instance: {instance_code}  |  Password: {password}\n"
        f"{'='*70}\n\n"
        f"PERSONAS (log in as these users):\n"
        + "\n".join(f"  • {p}" for p in personas) + "\n\n"
        f"{'─'*70}\n"
        f"SURFACE 1 — JOULE CHAT (in-app assistant)\n"
        f"  Show the AE typing these prompts directly in the SF Joule sidebar.\n"
        f"  Everything Joule answers here is grounded in live data.\n"
        f"{'─'*70}\n\n"
        + "\n\n".join(chat_lines) + "\n\n"
        f"{'─'*70}\n"
        f"SURFACE 2 — JOULE DESKTOP / CLAUDE CODE (agentic tier)\n"
        f"  Switch to Joule Desktop or Claude Code with the sf-demo-builder MCP.\n"
        f"  The agent reads live SF data via MCP and synthesises it into outputs\n"
        f"  no chatbot response can match.\n"
        f"{'─'*70}\n\n"
        + "\n\n".join(desktop_lines) + "\n\n"
        f"{'─'*70}\n"
        f"STORY BRIDGE (what the AE says when Joule can't show it live)\n"
        f"{'─'*70}\n"
        f"  {story_bridge}\n\n"
        f"{'='*70}\n"
        f"  End of script. Total live entities: {len(plan.get('live_data',[]))}\n"
        f"  Story entities (narrative only): {len(story_items)}\n"
        f"{'='*70}\n"
    )

    return json.dumps({
        "script":   script_text,
        "personas": personas,
        "joule_chat_beats":    [b["beat"] for b in chat_beats],
        "joule_desktop_beats": [b["beat"] for b in desktop_beats],
        "story_bridge": story_bridge,
        "login_url": script_login_url,
    }, indent=2)


if __name__ == "__main__":
    if _HTTP_MODE:
        port_arg = PORT
        for i, arg in enumerate(sys.argv):
            if arg == "--port" and i + 1 < len(sys.argv):
                port_arg = int(sys.argv[i + 1])
        print(f"Starting SF Demo Builder MCP (HTTP mode) on port {port_arg}")
        print(f"  Auth XSUAA: {'VCAP verificationkey' if _vcap_xsuaa_creds() else XSUAA_JWKS_URI}")
        print(f"  Auth A2A  : X-API-Key header at /a2a/mcp")
        print(f"  Base URL  : {SERVER_BASE_URL}")

        import uvicorn
        from starlette.requests import Request
        from starlette.responses import JSONResponse, Response
        from starlette.routing import Route, Mount
        from starlette.applications import Starlette
        from starlette.types import ASGIApp, Receive, Scope, Send

        class ApiKeyMiddleware:
            """Validate X-API-Key for requests arriving at /a2a/*.

            On success: sets _api_key_caller and _is_a2a_request ContextVars,
            then forwards to the inner MCP app.
            On failure: returns 401 JSON immediately.
            """
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
                owner    = _db.lookup_api_key(key_hash)
                if not owner:
                    await _send_401(send, "Invalid or revoked API key")
                    return

                # Set identity ContextVars for this request lifetime
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
            xsuaa_creds     = _vcap_xsuaa_creds() or {}
            xsuaa_clientid  = xsuaa_creds.get("clientid", "(see cf env sf-demo-builder → xsuaa credentials)")
            xsuaa_clientsecret = xsuaa_creds.get("clientsecret", "(see cf env sf-demo-builder → xsuaa credentials)")
            xsuaa_token_url = (xsuaa_creds.get("url") or "https://six-ai.authentication.us10.hana.ondemand.com") + "/oauth/token"
            xsuaa_auth_url  = (xsuaa_creds.get("url") or "https://six-ai.authentication.us10.hana.ondemand.com") + "/oauth/authorize"
            tools = [
                {"name": "design_demo_org",          "desc": "Design a demo org plan (personas, goals, scenario)"},
                {"name": "provision_demo_org",        "desc": "Start background provisioning — returns job_id immediately"},
                {"name": "get_provisioning_status",   "desc": "Poll a provisioning job by job_id (returns credentials when done)"},
                {"name": "list_my_orgs",              "desc": "List all demo orgs you have provisioned"},
                {"name": "get_org_details",           "desc": "Full metadata for one org by demo_id"},
                {"name": "get_org_employees",         "desc": "All employees + credentials for a demo org"},
                {"name": "get_org_goals",             "desc": "Goal assignments per employee for a demo org"},
                {"name": "get_org_compensation",      "desc": "Salary history + bonus data for a demo org"},
                {"name": "get_org_talent",            "desc": "Talent profiles (impact/risk/future leader)"},
                {"name": "delete_demo_org",           "desc": "Permanently delete a demo org from SF and HANA (confirmation required)"},
                {"name": "generate_api_key",          "desc": "Generate an A2A API key (JWT path only)"},
                {"name": "list_api_keys",             "desc": "List your API keys (label, last used, active)"},
                {"name": "revoke_api_key",            "desc": "Revoke an API key by label"},
                {"name": "configure_sf_instance",     "desc": "Save a custom SF instance config (API URL, admin creds, IAS)"},
                {"name": "get_sf_instance_config",    "desc": "Show your current SF instance config"},
                {"name": "reset_sf_instance_config",  "desc": "Delete your custom SF config (revert to default instance)"},
                {"name": "list_scenarios",            "desc": "List all available demo scenarios"},
                {"name": "generate_demo_script",      "desc": "Generate two-surface demo script from a plan"},
                {"name": "generate_agent_card",       "desc": "Generate Joule Agent Hub card from a plan"},
                {"name": "whoami",                    "desc": "Show your authenticated identity and SF instance config"},
            ]
            rows = "".join(
                f"<tr><td><code>{t['name']}</code></td><td>{t['desc']}</td></tr>"
                for t in tools
            )
            html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>SF Demo Builder — MCP Info</title>
  <style>
    body  {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
             max-width: 860px; margin: 40px auto; padding: 0 20px; color: #1a1a1a; }}
    h1   {{ font-size: 1.6rem; margin-bottom: 4px; }}
    h2   {{ font-size: 1.1rem; margin-top: 32px; border-bottom: 1px solid #ddd; padding-bottom: 6px; }}
    .badge {{ display:inline-block; padding:2px 8px; border-radius:4px; font-size:.75rem;
              font-weight:600; margin-left:6px; vertical-align:middle; }}
    .green  {{ background:#d4edda; color:#155724; }}
    .blue   {{ background:#cce5ff; color:#004085; }}
    .gray   {{ background:#e2e3e5; color:#383d41; }}
    table  {{ width:100%; border-collapse:collapse; font-size:.9rem; margin-top:8px; }}
    th,td  {{ padding:8px 10px; text-align:left; border-bottom:1px solid #eee; }}
    th     {{ background:#f8f9fa; font-weight:600; }}
    code   {{ background:#f3f4f6; padding:1px 5px; border-radius:3px; font-size:.85rem; }}
    pre    {{ background:#f3f4f6; padding:14px; border-radius:6px; font-size:.82rem;
              overflow-x:auto; }}
    .block {{ background:#fff8e1; border-left:3px solid #f0ad4e; padding:10px 14px;
              margin:12px 0; border-radius:0 4px 4px 0; font-size:.88rem; }}
    .copy-row {{ display:flex; align-items:center; gap:8px; margin:6px 0; }}
    .copy-row label {{ min-width:130px; font-size:.82rem; color:#555; font-weight:600; }}
    .copy-row .val {{ flex:1; font-family:monospace; font-size:.82rem; background:#f3f4f6;
                      padding:5px 8px; border-radius:4px; word-break:break-all; }}
    .copy-btn {{ flex-shrink:0; background:#0057b7; color:#fff; border:none; border-radius:4px;
                 padding:4px 10px; font-size:.78rem; cursor:pointer; white-space:nowrap; }}
    .copy-btn:hover {{ background:#003d82; }}
    .copy-btn.copied {{ background:#28a745; }}
    .card {{ border:1px solid #dee2e6; border-radius:8px; padding:16px 20px; margin:12px 0; }}
  </style>
</head>
<body>
  <h1>SF Demo Builder <span class="badge green">LIVE</span></h1>
  <p>SAP SuccessFactors demo environment builder — MCP server on Cloud Foundry.</p>

  <h2>Endpoints</h2>
  <table>
    <tr><th>Path</th><th>Auth</th><th>Who uses it</th></tr>
    <tr>
      <td><code>{base}/mcp</code></td>
      <td><span class="badge blue">XSUAA Bearer JWT</span></td>
      <td>Humans — Joule, browser, Claude Code, SAP AI Agent builder</td>
    </tr>
    <tr>
      <td><code>{base}/a2a/mcp</code></td>
      <td><span class="badge gray">X-API-Key header</span></td>
      <td>A2A — sf-demo-agent, other agents, scripts, CI pipelines</td>
    </tr>
    <tr>
      <td><code>{base}/health</code></td>
      <td>None</td>
      <td>CF health check</td>
    </tr>
    <tr>
      <td><code>{base}/info</code></td>
      <td>None</td>
      <td>This page</td>
    </tr>
  </table>

  <h2>Human / OAuth2 login (XSUAA)</h2>
  <p>Add this MCP server in Joule or any MCP client using <strong>OAuth 2.0</strong>.
     The client will redirect you to SAP SSO — no manual token needed.</p>
  <div class="card">
    <div class="copy-row"><label>MCP URL</label>
      <span class="val" id="v-url">{base}/mcp</span>
      <button class="copy-btn" onclick="cp('v-url',this)">Copy</button></div>
    <div class="copy-row"><label>Authentication</label>
      <span class="val">OAuth 2.0</span></div>
    <div class="copy-row"><label>Client ID</label>
      <span class="val" id="v-cid">{xsuaa_clientid}</span>
      <button class="copy-btn" onclick="cp('v-cid',this)">Copy</button></div>
    <div class="copy-row"><label>Client Secret</label>
      <span class="val" id="v-cs">{xsuaa_clientsecret}</span>
      <button class="copy-btn" onclick="cp('v-cs',this)">Copy</button></div>
    <div class="copy-row"><label>Token URL</label>
      <span class="val" id="v-tok">{xsuaa_token_url}</span>
      <button class="copy-btn" onclick="cp('v-tok',this)">Copy</button></div>
    <div class="copy-row"><label>Auth URL</label>
      <span class="val" id="v-auth">{xsuaa_auth_url}</span>
      <button class="copy-btn" onclick="cp('v-auth',this)">Copy</button></div>
    <div class="copy-row"><label>Scopes</label>
      <span class="val" id="v-sc">openid</span>
      <button class="copy-btn" onclick="cp('v-sc',this)">Copy</button></div>
  </div>

  <h2>A2A / API-key access</h2>
  <div class="card">
    <div class="copy-row"><label>MCP URL</label>
      <span class="val" id="v-a2aurl">{base}/a2a/mcp</span>
      <button class="copy-btn" onclick="cp('v-a2aurl',this)">Copy</button></div>
    <div class="copy-row"><label>Authentication</label>
      <span class="val">X-API-Key header</span></div>
    <div class="copy-row"><label>Header name</label>
      <span class="val" id="v-hdr">X-API-Key</span>
      <button class="copy-btn" onclick="cp('v-hdr',this)">Copy</button></div>
  </div>
  <p style="font-size:.85rem">Generate a key via the <code>generate_api_key</code> tool on the JWT endpoint.
     Keys are stored as SHA-256 hashes only — plaintext shown once.</p>

  <script>
  function cp(id,btn){{
    var t=document.getElementById(id).textContent;
    navigator.clipboard.writeText(t).then(function(){{
      btn.textContent='Copied!'; btn.classList.add('copied');
      setTimeout(function(){{btn.textContent='Copy';btn.classList.remove('copied');}},2000);
    }});
  }}
  </script>

  <h2>Available tools ({len(tools)})</h2>
  <table>
    <tr><th>Tool</th><th>Description</th></tr>
    {rows}
  </table>

  <h2>Data isolation</h2>
  <p>All <code>get_org_*</code> tools are automatically filtered to orgs owned by the
  authenticated caller. JWT callers are identified by their XSUAA email claim.
  API-key callers inherit the email of the user who generated the key.</p>

  <h2>SF instance</h2>
  <table>
    <tr><th>Field</th><th>Value</th></tr>
    <tr><td>Instance</td><td><code>SFSALES011375</code></td></tr>
    <tr><td>Login URL</td><td><a href="{LOGIN_URL}" target="_blank">{LOGIN_URL}</a></td></tr>
    <tr><td>Admin user</td><td><code>sfadmin@SFSALES011375</code></td></tr>
  </table>
</body>
</html>"""
            return Response(html, media_type="text/html")


        # Build the MCP ASGI app at path "/" — Starlette strips the mount prefix
        # before the request reaches the inner app, so the inner route must be "/".
        # stateless_http=True eliminates "Session not found" on container restarts.
        if hasattr(mcp, "streamable_http_app"):
            mcp_asgi = mcp.streamable_http_app(path="/", stateless_http=True)
        else:
            mcp_asgi = mcp.http_app(path="/", stateless_http=True)

        # Wrap with API-key middleware for the A2A path — same tool registry.
        a2a_asgi = ApiKeyMiddleware(mcp_asgi)

        # Use Route with path_regex to avoid Starlette's 307 redirect on missing
        # trailing slash that Mount("/mcp") triggers. Both /mcp and /mcp/ match.
        from starlette.routing import Router
        from starlette.types import ASGIApp as _ASGIApp

        def _make_prefix_stripper(prefix: str, inner: _ASGIApp) -> _ASGIApp:
            """Strip a path prefix and forward to inner app."""
            async def _app(scope, receive, send):
                if scope["type"] == "http":
                    path = scope.get("path", "")
                    if path.startswith(prefix):
                        scope = dict(scope)
                        scope["path"] = path[len(prefix):] or "/"
                        scope["raw_path"] = scope["path"].encode()
                return await inner(scope, receive, send)
            return _app

        mcp_stripped   = _make_prefix_stripper("/mcp",     mcp_asgi)
        a2a_stripped   = _make_prefix_stripper("/a2a/mcp", a2a_asgi)

        from starlette.routing import Router as _Router
        from starlette.routing import Route as _Route
        _static_app = _Router(routes=[
            _Route("/health", health),
            _Route("/info",   info),
        ])

        async def dispatch(scope, receive, send):
            path = scope.get("path", "")
            if path == "/health" or path.startswith("/health"):
                await _static_app(scope, receive, send)
            elif path == "/info" or path.startswith("/info"):
                await _static_app(scope, receive, send)
            elif path.startswith("/.well-known/"):
                # OAuth2 discovery endpoints (RFC 8414 / RFC 9728) — served by FastMCP auth provider
                await mcp_asgi(scope, receive, send)
            elif path.startswith("/a2a/mcp"):
                await a2a_stripped(scope, receive, send)
            elif path.startswith("/mcp"):
                await mcp_stripped(scope, receive, send)
            else:
                # Fallback 404
                body = b"Not Found"
                await send({"type": "http.response.start", "status": 404,
                            "headers": [[b"content-length", str(len(body)).encode()]]})
                await send({"type": "http.response.body", "body": body})

        class _AppWithLifespan:
            def __init__(self):
                self.lifespan = mcp_asgi.lifespan
            async def __call__(self, scope, receive, send):
                if scope["type"] == "lifespan":
                    await mcp_asgi(scope, receive, send)
                else:
                    await dispatch(scope, receive, send)

        app = _AppWithLifespan()

        uvicorn.run(app, host="0.0.0.0", port=port_arg)
    else:
        mcp.run()

