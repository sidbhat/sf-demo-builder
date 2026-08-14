"""Phase 2: Provision demo org into SuccessFactors."""

from __future__ import annotations

import json
import time
import threading
import uuid
import urllib.request
import urllib.parse
from typing import Optional
from datetime import datetime

import db as _db
from auth import _build_sf_config, _extract_caller_email, CTX
from sf_client import _sf_upsert, _sf_post
from scenarios import DEPT_NAMES, DEPT_DIVISION

# ── Background provisioning job store ────────────────────────────────────────
# job_id → {"status": "pending"|"running"|"done"|"error", "result": str|None,
#            "started_at": float, "error": str|None}
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


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
        return json.dumps({
            "error": "Org plan must have at least 2 employees (1 manager + 1 report). Please redesign with n_employees ≥ 2."
        })

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
            "payScaleArea": "USA/US1", "payScaleType": "USA/US1",
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
            "payGroup": "US",
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
    # Use uid6 to derive unique award code prefix — avoids collisions across orgs
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
        comment = sub.get("spot_award") or _default_award_msgs[min(i, 3)]
        amt = [2500.0, 1500.0, 1500.0, 1000.0][min(i, 3)]
        code = BASE_CODE + i + 1
        row = {
            "__metadata": {"uri": f"SpotAward({code})"},
            "externalCode": code,
            "userId":       sub["userId"],
            "nominatorId":  ceo_id,
            "awardAmount":  amt,
            "currency":     "USD",
            "commentForReceiver": comment,
            "category":     "Recognition",
            "approvalStatus": "Approved",
        }
        if _active_program:
            row["spotAwardProgram"] = _active_program
        award_rows.append(row)
    ok, errs = _sf_upsert(award_rows, sf)
    results["SpotAwards"] = f"{ok}/{len(award_rows)}"
    if errs:
        print(f"[spot_awards] errors codes={[r['externalCode'] for r in award_rows]} errs={errs[:3]}", flush=True)
    # Don't surface eligibility/program config errors as blocking failures
    non_eligibility_errs = [e for e in errs if not any(w in e.lower() for w in ("not eligible", "inactive", "expired"))]
    all_errors.extend(non_eligibility_errs)

    # ── Phase 14: Onboardee ────────────────────────────────────────────────────
    onb_uid  = f"{emp_prefix}{len(employees)+1:03d}"
    onb_user = f"{emp_prefix.lower()}.onb"
    onb_tag  = f"{emp_prefix.lower()}.onb"
    onb_email = f"{email_pfx}+{onb_tag}.{uid6}@sap.com"
    onb_mgr  = employees[1]["userId"]
    onb_dept = employees[1]["dept"]
    onb_pos  = employees[1]["position"]

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

    # Goals are complex; gracefully skip if they fail — org is still usable without them
    try:
        from ias_client import _ias_ensure_user
        for e in employees:
            uid = e["userId"]
            username = e["username"]
            email = f"{email_pfx}+{e['email_tag']}.{uid6}@sap.com"

            # Ensure user exists in IAS (only if IAS is configured)
            if sf.get("ias_scim_url"):
                _ias_ensure_user(username, password, email, sf)

            # Post annual goal (Goal_11)
            try:
                ok_goal1, _ = _sf_post_as_user("Goal_11", {
                    "userId": uid, "type": "user",
                    "name": e["goals"]["annual_1_name"],
                    "metric": e["goals"]["annual_1_metric"],
                    "description": e["goals"]["annual_1_name"],
                    "category": "Goals", "state": "On Track", "done": 0,
                    "start": GOAL_START, "due": GOAL_DUE,
                }, username, password, sf)
                if ok_goal1:
                    goals_ok += 1
            except Exception as _e:
                print(f"[goals] annual goal for {uid} failed: {_e}", flush=True)
            goals_total += 1

            # Post dev goal (DevGoal_2001)
            try:
                ok_goal2, _ = _sf_post_as_user("DevGoal_2001", {
                    "userId": uid, "type": "development",
                    "name": e["goals"]["dev_name"],
                    "metric": e["goals"]["dev_metric"],
                    "purpose": "Current role",
                    "category": "Goals", "state": "On Track",
                    "start": GOAL_START, "due": GOAL_DUE,
                    "competencies": {"results": []},
                }, username, password, sf)
                if ok_goal2:
                    goals_ok += 1
            except Exception as _e:
                print(f"[goals] dev goal for {uid} failed: {_e}", flush=True)
            goals_total += 1
    except Exception as e:
        print(f"[goals] skipping goals provisioning: {e}", flush=True)

    results["Goals"] = f"{goals_ok}/{goals_total}"

    # ── Provision complete ─────────────────────────────────────────────────────
    confirmation = {
        "status": "provisioned",
        "demo_id": demo_id,
        "company_code": co,
        "org_name": name,
        "employees_created": len(employees),
        "onboardee": f"Sam Rivera ({onb_uid}, +3 weeks)",
        "login_url": sf["login_url"],
        "default_password": password,
        "phase_results": results,
        "credentials": {
            "username": e["username"],  # Last employee in loop
            "email": f"{email_pfx}+{e['email_tag']}.{uid6}@sap.com",
            "password": password,
        },
        "caveats": (
            "Phase_results show success/total for each entity type. "
            "If not all succeeded, check all_errors below for details."
        ),
        "all_errors": all_errors[:20],  # Limit to first 20 errors
    }

    return json.dumps(confirmation, indent=2)


def register(mcp):
    """Register provision tools with mcp instance."""
    mcp.tool()(provision_demo_org)
    mcp.tool()(get_provisioning_status)
