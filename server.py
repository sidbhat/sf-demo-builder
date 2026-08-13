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
import ssl
import sys
import urllib.request
import urllib.parse
import time
import os
from datetime import datetime
from typing import Optional

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

# Build auth provider when running in HTTP mode (--http flag)
# In stdio mode (default, used by Claude Code MCP) auth is bypassed —
# the local process boundary is the trust boundary.
_HTTP_MODE = "--http" in sys.argv

def _build_auth():
    if not _HTTP_MODE:
        return None
    from fastmcp.server.auth import RemoteAuthProvider, JWTVerifier
    verifier = JWTVerifier(
        jwks_uri=XSUAA_JWKS_URI,
        issuer=XSUAA_ISSUER,
        algorithm="RS256",
    )
    return RemoteAuthProvider(
        token_verifier=verifier,
        authorization_servers=[XSUAA_ISSUER],
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

# ── Principal propagation helpers ─────────────────────────────────────────────

def _extract_caller_email(ctx=None) -> Optional[str]:
    """Extract SAP email from the validated XSUAA JWT context.

    In HTTP mode FastMCP puts the verified AccessToken on the request context.
    Returns None in stdio mode (no auth token present).
    """
    if not _HTTP_MODE:
        return None
    try:
        from fastmcp import Context
        if ctx and hasattr(ctx, "auth_context") and ctx.auth_context:
            claims = getattr(ctx.auth_context, "extra", {}) or {}
            return claims.get("email") or claims.get("user_name")
    except Exception:
        pass
    return None


def _alias_from_email(email: str) -> Optional[str]:
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

def _sf_upsert(rows: list) -> tuple[int, list]:
    """Returns (ok_count, error_messages)."""
    if not rows:
        return 0, []
    body = json.dumps(rows).encode()
    req = urllib.request.Request(f"{SF_BASE}/upsert", data=body, headers=SF_HEADERS, method="POST")
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


def _sf_post(entity: str, row: dict) -> tuple[bool, str]:
    body = json.dumps(row).encode()
    req = urllib.request.Request(f"{SF_BASE}/{entity}", data=body, headers=SF_HEADERS, method="POST")
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


def _sf_post_as_user(entity: str, row: dict, username: str, password: str) -> tuple[bool, str]:
    """POST to SF OData authenticated as a specific user (required for Goal entities)."""
    creds = base64.b64encode(f"{username}@SFSALES011375:{password}".encode()).decode()
    hdrs = {**SF_HEADERS, "Authorization": f"Basic {creds}"}
    body = json.dumps(row).encode()
    req = urllib.request.Request(
        f"https://apisalesdemo8.successfactors.com/odata/v2/{entity}",
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
        if "already exists" in msg.lower() or "duplicate" in msg.lower():
            return True, "already_exists"
        return False, msg[:120]


def _ias_get_user(username: str):
    url = IAS_SCIM_URL + "?filter=" + urllib.parse.quote(f'userName eq "{username}"')
    req = urllib.request.Request(url, headers={"Authorization": IAS_AUTH, "Accept": "application/scim+json"})
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=15) as r:
            d = json.loads(r.read())
            return (d.get("Resources") or [None])[0]
    except Exception:
        return None


def _ias_set_password(username: str, password: str, email: str) -> tuple[bool, str]:
    user = _ias_get_user(username)
    if not user:
        return False, "IAS user not found"
    scim_id = user["id"]
    user["password"] = password
    user["emails"] = [{"value": email, "primary": True, "type": "work"}]
    for f in ["meta", "groups"]:
        user.pop(f, None)
    payload = json.dumps(user).encode()
    req = urllib.request.Request(
        f"{IAS_SCIM_URL}/{scim_id}", data=payload, method="PUT",
        headers={"Authorization": IAS_AUTH, "Content-Type": "application/scim+json",
                 "Accept": "application/scim+json"})
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=15) as r:
            return True, str(r.status)
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode(errors='replace')[:100]}"


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
    password: str = "MarsD2025",
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
        company_name:     Customer/company name (e.g. "Nike", "Siemens Energy")
        industry:         Industry vertical: retail, tech, manufacturing, healthcare,
                          financial_services, energy
        country:          Country code for locale/currency: USA, GBR, DEU, FRA, IND,
                          AUS, SGP, BRA
        business_problem: The SF domain scenario: mass_hiring, compensation_planning,
                          talent_retention, skills_learning, performance_goals,
                          workforce_planning, succession_prep, pay_equity_deep_dive,
                          onboarding_readiness
        n_employees:      Number of employees to create (currently 5 supported)
        company_code:     4-digit SF company code (auto-assigned if omitted)
        employee_prefix:  2-3 char userId prefix (e.g. "NK" for Nike)
        email_prefix:     Email address prefix for +alias users (defaults to caller's
                          email prefix via principal propagation, else env/config)
        password:         Default login password for all users
    """
    # Principal propagation: pull email from XSUAA JWT if available
    caller_email = _extract_caller_email(ctx)
    caller_alias = _alias_from_email(caller_email) if caller_email else None

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
    if industry_key not in INDUSTRY_ROLES:
        available = list(INDUSTRY_ROLES.keys())
        return json.dumps({"error": f"Unknown industry '{industry}'. Available: {available}"})

    if problem_key not in SCENARIO_KB:
        available = list(SCENARIO_KB.keys())
        return json.dumps({"error": f"Unknown problem '{business_problem}'. Available: {available}"})

    if country_key not in LOCALE_CONFIG:
        country_key = "USA"  # default fallback

    locale = LOCALE_CONFIG[country_key]
    scenario = SCENARIO_KB[problem_key]
    roles = INDUSTRY_ROLES.get(industry_key, INDUSTRY_ROLES["tech"])

    # Auto-assign company code if not given (hash company name to 4-digit range 5000-9000)
    if not company_code:
        company_code = str(5000 + (hash(company_name) % 4000))

    # Auto-assign employee prefix from company name
    if not employee_prefix:
        words = company_name.upper().split()
        if len(words) >= 2:
            employee_prefix = words[0][:1] + words[1][:1]
        else:
            employee_prefix = company_name.upper()[:3]

    # Build employee roster
    role_keys = list(roles.keys())[:n_employees]
    employees = []
    ceo_id = None
    for i, rk in enumerate(role_keys):
        num, short, title, dept, grade = roles[rk]
        uid      = f"{employee_prefix}{num}"
        username = f"{employee_prefix.lower()}.{rk}"
        fn, ln   = FIRST_NAMES.get(rk, (f"User{num}", "Smith"))
        mgr      = None if i == 0 else (ceo_id or f"{employee_prefix}{roles[role_keys[0]][0]}")
        if i == 0:
            ceo_id = uid
        sal = SALARY_HISTORY.get(grade, [90000, 97000, 105000])
        impact, risk, fl = GRADE_IMPACT.get(grade, ("MEDIUM", "LOW", False))
        bonus = BONUS_BY_GRADE.get(grade, 8000)
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
    scenario_live = set(scenario["live_data"])
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
    for key, status, desc in story_entities:
        if key in scenario["story_data"]:
            story_items.append({"status": status, "entity": key, "description": desc})

    card = scenario["agent_card"]
    plan = {
        "plan_version":    "1.0",
        "company_name":    company_name,
        "company_code":    company_code,
        "industry":        industry_key,
        "country":         country_key,
        "locale":          locale,
        "business_problem": problem_key,
        "scenario_label":  scenario["label"],
        "employee_prefix": employee_prefix,
        "email_prefix":    email_prefix,
        "password":        password,
        "n_employees":     len(employees),
        "employees":       employees,
        "org_chart": "\n".join(org_lines),
        "scenario_narrative": scenario["talent_story"],
        "story_data_narrative": scenario["story_narrative"],
        "joule_prompts":   scenario["joule_prompts"],
        "live_data":       live_items,
        "story_data":      story_items,
        # Principal propagation: who called this and which persona they map to
        "caller": {
            "email":       caller_email,
            "sf_alias":    caller_alias,
            "sf_login":    f"{caller_alias}@SFSALES011375" if caller_alias else None,
            "note": (
                f"Caller identified as {caller_alias} via XSUAA principal propagation."
                if caller_alias
                else "No XSUAA token — running in stdio/local mode."
            ),
        },
        "agent_card": {
            "title":     f"{company_name} — {card['title']}",
            "challenge": card["challenge"],
            "prompts":   card["prompts"],
            "live_count":  len(live_items),
            "story_count": len(story_items),
            "joule_url":   LOGIN_URL,
        },
        "summary": (
            f"{company_name} ({industry_key}, {country_key}) — {scenario['label']}\n"
            f"  {len(employees)} employees, company code {company_code}, prefix {employee_prefix}\n"
            f"  Password: {password}\n"
            f"  Login: {LOGIN_URL}\n\n"
            f"  LIVE ({len(live_items)} entities): {', '.join(i['entity'] for i in live_items)}\n"
            f"  STORY ({len(story_items)} entities): {', '.join(i['entity'] for i in story_items)}\n\n"
            f"  Story arc: {scenario['talent_story']}\n\n"
            f"  To provision this plan, call provision_demo_org() with this plan object."
        ),
    }

    return json.dumps(plan, indent=2)


# ── Phase 2: Provision ────────────────────────────────────────────────────────

@mcp.tool()
def provision_demo_org(plan_json: str) -> str:
    """
    Phase 2: Provision the org plan from design_demo_org() into SF SFSALES011375.

    Takes the JSON output of design_demo_org() and creates all LIVE entities.
    Reports each phase result and clearly marks what was created vs what is story.

    Args:
        plan_json: The full JSON string returned by design_demo_org()
    """
    try:
        plan = json.loads(plan_json)
    except Exception as e:
        return json.dumps({"error": f"Invalid plan JSON: {e}"})

    co          = plan["company_code"]
    name        = plan["company_name"]
    emp_prefix  = plan["employee_prefix"]
    employees   = plan["employees"]
    email_pfx   = plan["email_prefix"]
    password    = plan["password"]
    locale      = plan["locale"]
    industry    = plan["industry"]

    HIRE_DATE   = "/Date(1735689600000)/"   # 2025-01-01
    HIRE_STR    = "2025-01-01T00:00:00"
    BONUS_DATE  = "/Date(1766620800000)/"   # 2025-12-25
    BONUS_STR   = "2025-12-25T00:00:00"
    START_DATE  = "/Date(-2208988800000)/"  # 1900-01-01

    results = {}
    all_errors = []

    # ── Phase 1: FOCompany ─────────────────────────────────────────────────────
    ok, errs = _sf_upsert([{
        "__metadata": {"uri": "FOCompany"},
        "externalCode": co, "startDate": START_DATE,
        "name": name, "currency": locale["currency"],
        "country": locale["country_code"], "standardHours": 40, "status": "A",
    }])
    results["FOCompany"] = f"{ok}/1"
    all_errors.extend(errs)

    # ── Phase 2: FOCostCenter ──────────────────────────────────────────────────
    dept_keys = list(dict.fromkeys(e["dept_key"] for e in employees))
    cc_ok = 0
    for dk in dept_keys:
        ok2, _ = _sf_post("FOCostCenter", {
            "externalCode": f"{co}-{dk}", "startDate": START_DATE,
            "name": f"{name} {DEPT_NAMES.get(dk, dk)}", "status": "A",
        })
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
    ok, errs = _sf_upsert(dept_rows)
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
    }])
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
    ok, errs = _sf_upsert(pos_rows)
    results["Position"] = f"{ok}/{len(pos_rows)}"
    all_errors.extend(errs)

    # ── Phase 6: Users ─────────────────────────────────────────────────────────
    user_rows = []
    for e in employees:
        email = f"{email_pfx}+{e['email_tag']}@sap.com"
        user_rows.append({
            "__metadata": {"uri": f"User('{e['userId']}')"},
            "userId": e["userId"], "username": e["username"],
            "firstName": e["firstName"], "lastName": e["lastName"],
            "email": email, "status": "t",
            "defaultLocale": locale["locale"],
            "timeZone": locale["tz"],
            "password": password,
        })
    ok, errs = _sf_upsert(user_rows)
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
    ok, errs = _sf_upsert(emp_rows)
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
    ok, errs = _sf_upsert(job_rows)
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
    ok, errs = _sf_upsert(per_rows)
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
    ok_comp, errs = _sf_upsert(comp_rows)
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
    ok_sal, errs = _sf_upsert(sal_rows)
    all_errors.extend(errs)
    results["EmpCompensation+SalaryHistory"] = f"comp={ok_comp}/{len(comp_rows)} salary={ok_sal}/{len(sal_rows)}"

    # ── Phase 11: Year-end bonus via EmpPayCompRecurring ──────────────────────
    bonus_rows = []
    for e in employees:
        uid = e["userId"]
        bonus_rows.append({
            "__metadata": {"uri": f"EmpPayCompRecurring(payComponent='BASESAL_US',seqNumber=1L,startDate=datetime'{BONUS_STR}',userId='{uid}')"},
            "payComponent": "BASESAL_US", "userId": uid,
            "startDate": BONUS_DATE, "seqNumber": 1,
            "paycompvalue": float(e["yearEndBonus"]),
            "currencyCode": locale["currency"],
        })
    ok, errs = _sf_upsert(bonus_rows)
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
    ok, errs = _sf_upsert(talent_rows)
    results["TalentProfile"] = f"{ok}/{len(talent_rows)}"
    all_errors.extend(errs)

    # ── Phase 13: Spot awards (WOW Awards! — no eligibility restriction) ──────
    BASE_CODE = 800000 + int(co)
    AWARD_DATA = [
        (300, "Outstanding delivery — shipped ahead of schedule and under budget",
               "Above guideline: critical milestone warranted top recognition."),
        (200, "Strategic win — resolved a key risk that was blocking the roadmap",
               "Above guideline: impact was significant and time-sensitive."),
        (200, "Customer milestone — first major deal or delivery secured",
               "Above guideline: strategic inflection point for the business."),
        (100, "Operational excellence — consistent high-quality execution all year", None),
    ]
    award_rows = []
    ceo_id = employees[0]["userId"]
    for i, (sub, (pts, comment, approver_note)) in enumerate(
            zip(employees[1:], AWARD_DATA)):
        code = BASE_CODE + i + 1
        row = {
            "__metadata": {"uri": f"SpotAward({code})"},
            "externalCode": code,
            "userId":       sub["userId"],
            "nominatorId":  ceo_id,
            "awardAmount":  float(pts),
            "currency":     "POINTS",
            "spotAwardProgram": "WOW Awards!",
            "category":     "1",
            "level":        "1",
            "approvalStatus": "APPROVED",
            "commentForReceiver": comment,
        }
        if approver_note:
            row["commentForApprovers"] = approver_note
        award_rows.append(row)
    ok, errs = _sf_upsert(award_rows)
    results["SpotAwards"] = f"{ok}/{len(award_rows)}"
    # Don't surface spot award eligibility errors — graceful
    non_eligibility_errs = [e for e in errs if "not eligible" not in e.lower()]
    all_errors.extend(non_eligibility_errs)

    # ── Phase 14: Onboardee ────────────────────────────────────────────────────
    onb_uid  = f"{emp_prefix}{len(employees)+1:03d}"
    onb_user = f"{emp_prefix.lower()}.onb"
    onb_tag  = f"{emp_prefix.lower()}.onb"
    onb_email = f"{email_pfx}+{onb_tag}@sap.com"
    onb_mgr  = employees[1]["userId"]
    onb_dept = employees[1]["dept"]
    onb_pos  = employees[1]["position"]
    ONB_DATE  = "/Date(1762128000000)/"   # 2025-11-03
    ONB_STR   = "2025-11-03T00:00:00"

    ok1, _ = _sf_upsert([{
        "__metadata": {"uri": f"User('{onb_uid}')"},
        "userId": onb_uid, "username": onb_user,
        "firstName": "Sam", "lastName": "Rivera",
        "email": onb_email, "status": "t",
        "defaultLocale": locale["locale"], "timeZone": locale["tz"],
        "password": password,
    }])
    try:
        onb_payload = json.dumps({
            "userId": onb_uid, "firstName": "Sam", "lastName": "Rivera",
            "email": onb_email, "hireDate": ONB_DATE,
        }).encode()
        req = urllib.request.Request(f"{SF_BASE}/createOnboardee", data=onb_payload, headers=SF_HEADERS, method="POST")
        with urllib.request.urlopen(req, context=CTX, timeout=30):
            onb_fn_ok = 1
    except Exception:
        onb_fn_ok = 0

    ok3, _ = _sf_upsert([{
        "__metadata": {"uri": f"EmpEmployment(personIdExternal='{onb_uid}',userId='{onb_uid}')"},
        "personIdExternal": onb_uid, "userId": onb_uid,
        "startDate": ONB_DATE, "originalStartDate": ONB_DATE,
        "firstDateWorked": ONB_DATE, "seniorityDate": ONB_DATE,
    }])
    ok4, _ = _sf_upsert([{
        "__metadata": {"uri": f"EmpJob(seqNumber=1L,startDate=datetime'{ONB_STR}',userId='{onb_uid}')"},
        "userId": onb_uid, "seqNumber": 1,
        "startDate": ONB_DATE, "company": co,
        "department": onb_dept, "division": employees[1]["division"],
        "businessUnit": employees[1]["bu"],
        "employeeClass": "4662", "employmentType": "3631", "eventReason": "HIRNEW",
        "fte": 1.0, "jobCode": "50000724", "jobTitle": "Senior Associate",
        "location": loc_code, "managerId": onb_mgr, "payGrade": "GR-11",
        "payScaleArea": locale["pay_scale"], "payScaleType": locale["pay_scale"],
        "position": onb_pos, "standardHours": 40, "timezone": locale["tz"],
        "workscheduleCode": "NORM", "timeTypeProfileCode": "USA_STD",
        "holidayCalendarCode": "USA", "timeRecordingProfileCode": "DUR_NEG",
        "timeRecordingVariant": "DURATION",
        "timeRecordingAdmissibilityCode": "4WK_AMEND_YES",
        "defaultOvertimeCompensationVariant": "OCV_NO_PAYOUT",
    }])
    ok5, _ = _sf_upsert([{
        "__metadata": {"uri": f"PerPersonal(personIdExternal='{onb_uid}',startDate=datetime'{ONB_STR}')"},
        "personIdExternal": onb_uid, "startDate": ONB_DATE,
        "firstName": "Sam", "lastName": "Rivera",
        "nationality": locale["country_code"], "gender": "U",
        "maritalStatus": "10820", "nativePreferredLang": "10240",
    }])
    onb_success = all([ok1, ok3, ok4, ok5])
    results["Onboardee"] = f"{'1/1' if onb_success else '0/1'} ({onb_uid} Sam Rivera, Nov 3 start)"

    # ── Phase 15: Goals (Goal_11 annual + DevGoal_2001 dev goals) ────────────────
    # Goals must be created as the user themselves — sfadmin gets 403.
    # We can only create goals once IAS passwords are set (Phase 16).
    # Strategy: create goals after IAS setup, or skip if IAS not ready yet.
    # For now: create goals using the SF user credentials (username@SFSALES011375:password).
    goals_ok = 0
    goals_total = 0
    GOAL_START  = "/Date(1735689600000)/"   # 2025-01-01
    GOAL_DUE    = "/Date(1767225600000)/"   # 2025-12-31

    for e in employees:
        role_key = e.get("username", "").split(".")[-1] if "." in e.get("username", "") else ""
        g1n, g1m, g2n, g2m, dgn, dgm = GOAL_CONTENT.get(role_key, _DEFAULT_GOAL)

        annual_goals = [
            {"name": g1n, "metric": g1m},
            {"name": g2n, "metric": g2m},
        ]
        for goal in annual_goals:
            goals_total += 1
            ok, _ = _sf_post_as_user("Goal_11", {
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
            }, e["username"], password)
            if ok:
                goals_ok += 1

        goals_total += 1
        ok, _ = _sf_post_as_user("DevGoal_2001", {
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
        }, e["username"], password)
        if ok:
            goals_ok += 1

    results["Goals"] = f"{goals_ok}/{goals_total} (Goal_11 x2 + DevGoal_2001 x1 per employee)"

    # ── Phase 16: IAS passwords ────────────────────────────────────────────────
    all_users = employees + [{"username": onb_user, "email_tag": onb_tag}]
    ias_ok = 0
    # Poll up to 120s for first user to appear in IAS
    first_uname = employees[0]["username"]
    for attempt in range(20):
        ias_user = _ias_get_user(first_uname)
        if ias_user:
            break
        time.sleep(6)

    for e in all_users:
        email = f"{email_pfx}+{e['email_tag']}@sap.com"
        success, _ = _ias_set_password(e["username"], password, email)
        if success:
            ias_ok += 1
        time.sleep(1)
    results["IASPasswords"] = f"{ias_ok}/{len(all_users)}"

    # ── Build final confirmation ───────────────────────────────────────────────
    user_lines = []
    for e in employees:
        email = f"{email_pfx}+{e['email_tag']}@sap.com"
        mgr_label = "(root)" if e["manager"] is None else f"→ {e['manager']}"
        user_lines.append(
            f"  {e['userId']:<8} {e['username']:<22} {e['jobTitle']:<30} {mgr_label}\n"
            f"           email: {email}  sal: ${e['salaryHistory'][-1]:,}  bonus: ${e['yearEndBonus']:,}"
        )
    user_lines.append(
        f"  {onb_uid:<8} {onb_user:<22} Senior Associate (onboardee, starts Nov 3)"
    )

    story_lines = []
    for item in plan.get("story_data", []):
        story_lines.append(f"  {item['status']} {item['entity']}: {item['description']}")

    joule_lines = [f"  • {p}" for p in plan.get("joule_prompts", [])]

    output = {
        "status": "SUCCESS" if not all_errors else f"DONE WITH {len(all_errors)} ERROR(S)",
        "company":    name,
        "code":       co,
        "industry":   plan["industry"],
        "problem":    plan["scenario_label"],
        "login_url":  LOGIN_URL,
        "password":   password,
        "phase_results": results,
        "errors":     all_errors[:10],
        "confirmation": (
            f"{'='*64}\n"
            f"  {name} ({co}) — {plan['scenario_label']}\n"
            f"  Instance : SFSALES011375\n"
            f"  Login    : {LOGIN_URL}\n"
            f"  Password : {password}\n"
            f"{'='*64}\n\n"
            f"EMPLOYEES CREATED:\n" + "\n".join(user_lines) + "\n\n"
            f"WHAT'S LIVE IN SF:\n"
            + "\n".join(f"  ✅ {k}: {v}" for k, v in results.items()) + "\n\n"
            f"WHAT'S STORY (not provisioned):\n"
            + ("\n".join(story_lines) if story_lines else "  (none for this scenario)") + "\n\n"
            f"STORY NARRATIVE:\n  {plan.get('story_data_narrative','')}\n\n"
            f"JOULE PROMPTS TO TRY:\n" + "\n".join(joule_lines) + "\n"
            f"{'='*64}\n"
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


# ── Tool: Generate Agent Hub card ─────────────────────────────────────────────

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

    # Build persona table — who to log in as for the demo
    personas = []
    for e in employees[:3]:
        personas.append({
            "name":     f"{e['firstName']} {e['lastName']}",
            "title":    e["jobTitle"],
            "username": e["username"],
            "grade":    e["payGrade"],
            "login":    f"{e['username']}@SFSALES011375",
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
            "joule_url": LOGIN_URL,
            "password":  plan.get("password", ""),
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
            + f"│ {'Login: ' + LOGIN_URL[:53]:<60} │\n"
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
            f"— login: {e['username']}@SFSALES011375 / {password}"
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
        f"  Instance: SFSALES011375  |  Password: {password}\n"
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
        "login_url": LOGIN_URL,
    }, indent=2)


if __name__ == "__main__":
    if _HTTP_MODE:
        # HTTP + SSE transport with OAuth2 auth
        # Usage: python3 server.py --http [--port 8000]
        port_arg = PORT
        for i, arg in enumerate(sys.argv):
            if arg == "--port" and i + 1 < len(sys.argv):
                port_arg = int(sys.argv[i + 1])
        print(f"Starting SF Demo Builder MCP (HTTP mode) on port {port_arg}")
        print(f"  Auth:       XSUAA JWT — {XSUAA_JWKS_URI}")
        print(f"  Issuer:     {XSUAA_ISSUER}")
        print(f"  Base URL:   {SERVER_BASE_URL}")
        mcp.run(transport="streamable-http", host="0.0.0.0", port=port_arg, path="/mcp")
    else:
        # Default: stdio transport for Claude Code / Joule Desktop MCP
        mcp.run()
