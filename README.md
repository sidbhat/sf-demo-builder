# SF Demo Builder

MCP server that provisions isolated SAP SuccessFactors demo orgs on demand, then generates two-surface demo scripts for Joule Chat and Joule Desktop / Claude Code.

## What it does

**Phase 1 — Design** (`design_demo_org`): Given a company name, industry, country, and business problem, returns a complete org plan: roles, scenario narrative, live vs story capability manifest, and an Agent Hub card.

**Phase 2 — Provision** (`provision_demo_org`): Takes the plan and creates everything possible via SF OData in an isolated company code on SFSALES011375 — employees, org hierarchy, compensation history, talent profiles, succession nominations, goals, spot awards, and IAS logins.

**Supporting tools:**
- `list_scenarios` — browse all 9 business problem scenarios
- `generate_agent_card` — Joule Agent Hub-style card for a plan
- `generate_demo_script` — two-surface demo script: Joule Chat beats + Joule Desktop / Claude Code agentic beats, per scenario

## Auth — XSUAA principal propagation

In HTTP mode (`--http`), the server validates SAP Bearer JWTs against `accounts.sap.com` (RS256, JWKS). The caller's email is extracted from the token and the `+alias` suffix maps to an SF username:

```
siddhartha.bhattacharya+se.ceo@sap.com  →  se.ceo@SFSALES011375
```

This lets Joule Desktop / Claude Code identify which SF user is running the demo and default personas accordingly.

In stdio mode (default, used by Claude Code's MCP integration), auth is bypassed — the local process boundary is the trust boundary.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env — fill in SF_ADMIN_PASS, IAS_CLIENT_SECRET, EMAIL_PREFIX
```

## Run modes

```bash
# stdio mode (Claude Code MCP — default)
python3 server.py

# HTTP mode (remote / Joule Desktop HTTP MCP)
python3 server.py --http --port 8000

# Generate a local dev token (for testing HTTP mode without a real SAP token)
python3 generate_token.py --email your.name+se.ceo@sap.com
```

## Register with Claude Code

`~/.claude/settings.json`:
```json
{
  "mcpServers": {
    "sf-demo-builder": {
      "command": "python3.11",
      "args": ["/path/to/sf-demo-builder/server.py"]
    }
  }
}
```

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `XSUAA_JWKS_URI` | `https://accounts.sap.com/oauth2/certs` | JWKS endpoint for JWT validation |
| `XSUAA_ISSUER` | `https://accounts.sap.com` | Expected JWT issuer |
| `SERVER_BASE_URL` | `http://localhost:8000` | Public URL of this server |
| `PORT` | `8000` | HTTP listen port |
| `SF_ADMIN_USER` | `sfadmin@SFSALES011375` | SF admin username |
| `SF_ADMIN_PASS` | *(required)* | SF admin password |
| `IAS_CLIENT_ID` | *(required)* | IAS SCIM system admin client ID |
| `IAS_CLIENT_SECRET` | *(required)* | IAS SCIM system admin client secret |
| `EMAIL_PREFIX` | `siddhartha.bhattacharya` | Email local-part before `+alias` |

## Industries and scenarios

**Industries:** `retail`, `tech`, `manufacturing`, `healthcare`, `financial_services`, `energy`

**Business problems:** `mass_hiring`, `compensation_planning`, `talent_retention`, `skills_learning`, `performance_goals`, `workforce_planning`, `succession_prep`, `pay_equity_deep_dive`, `onboarding_readiness`
