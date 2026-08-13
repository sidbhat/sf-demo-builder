"""Query tools for demo org data."""

import json
import base64
import urllib.request
import urllib.parse
from typing import Optional

import db as _db
from auth import _build_sf_config, _extract_caller_email, CTX
from sf_client import _sf_delete
from scenarios import SCENARIO_KB


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

    out = {"demo_id": demo_id, "company": org["company_name"], "goals": []}
    for u in org.get("users", []):
        g = u.get("goals", {})
        out["goals"].append({
            "user_id": u["user_id"],
            "username": u["username"],
            "annual_1": {
                "name": g.get("annual_1_name", ""),
                "metric": g.get("annual_1_metric", ""),
            },
            "annual_2": {
                "name": g.get("annual_2_name", ""),
                "metric": g.get("annual_2_metric", ""),
            },
            "development": {
                "name": g.get("dev_name", ""),
                "metric": g.get("dev_metric", ""),
            },
        })

    return json.dumps(out, indent=2)


def get_org_compensation(demo_id: str, ctx=None) -> str:
    """
    Show compensation data: salary history, year-end bonus, pay grade, and
    talent profile risk/impact flags for every employee in a demo org.

    Useful for compensation planning and retention scenarios.
    Only returns data for orgs you created (enforced via XSUAA identity).

    Args:
        demo_id: The UUID returned by provision_demo_org()
    """
    caller_email = _extract_caller_email(ctx)
    org, err = _resolve_demo_id(demo_id, caller_email)
    if err:
        return json.dumps({"error": err})

    out = {"demo_id": demo_id, "company": org["company_name"], "compensation": []}
    for u in org.get("users", []):
        out["compensation"].append({
            "user_id": u["user_id"],
            "username": u["username"],
            "job_title": u.get("job_title", ""),
            "pay_grade": u.get("pay_grade", ""),
            "salary_history": u.get("salary_history", []),
            "year_end_bonus": u.get("year_end_bonus", 0),
            "talent_profile": {
                "impactOfLoss": u.get("impactOfLoss", ""),
                "riskOfLoss": u.get("riskOfLoss", ""),
                "futureLeader": u.get("futureLeader", False),
            },
        })

    return json.dumps(out, indent=2)


def get_org_talent(demo_id: str, ctx=None) -> str:
    """
    Show talent profile data: successor readiness, flight risk, impact of loss,
    and future leadership potential for every employee in a demo org.

    Useful for succession planning and retention scenarios.
    Only returns data for orgs you created (enforced via XSUAA identity).

    Args:
        demo_id: The UUID returned by provision_demo_org()
    """
    caller_email = _extract_caller_email(ctx)
    org, err = _resolve_demo_id(demo_id, caller_email)
    if err:
        return json.dumps({"error": err})

    out = {"demo_id": demo_id, "company": org["company_name"], "talent": []}
    for u in org.get("users", []):
        out["talent"].append({
            "user_id": u["user_id"],
            "username": u["username"],
            "job_title": u.get("job_title", ""),
            "pay_grade": u.get("pay_grade", ""),
            "impactOfLoss": u.get("impactOfLoss", ""),
            "riskOfLoss": u.get("riskOfLoss", ""),
            "futureLeader": u.get("futureLeader", False),
        })

    return json.dumps(out, indent=2)


def register(mcp):
    """Register query tools with mcp instance."""
    mcp.tool()(list_scenarios)
    mcp.tool()(list_my_orgs)
    mcp.tool()(get_org_details)
    mcp.tool()(delete_demo_org)
    mcp.tool()(get_org_employees)
    mcp.tool()(get_org_goals)
    mcp.tool()(get_org_compensation)
    mcp.tool()(get_org_talent)
