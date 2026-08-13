"""Phase 1: Design demo org tool."""

from __future__ import annotations

import json
import secrets
from typing import Optional

from auth import _build_sf_config, _extract_caller_email, _alias_from_email
from scenarios import (
    SCENARIO_KB, LOCALE_CONFIG, GOAL_CONTENT, DEPT_NAMES, DEPT_DIVISION,
    INDUSTRY_ROLES, _DEFAULT_GOAL, FIRST_NAMES, SALARY_HISTORY, BONUS_BY_GRADE,
    GRADE_IMPACT, DEPT_BU
)


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
    from auth import DEFAULT_EMAIL_PREFIX

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


def register(mcp):
    """Register design_demo_org tool with mcp instance."""
    mcp.tool()(design_demo_org)
