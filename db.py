"""
HANA persistence layer for SF Demo Builder.

Tables (created on first connect if not present):
  DEMO_ORGS        — one row per provisioned demo org
  DEMO_ORG_EMAILS  — one row per user email in that org

Connection comes from VCAP_SERVICES (CF) or env vars (local).
"""

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

# ── Connection factory ────────────────────────────────────────────────────────

def _vcap_hana_creds() -> Optional[dict]:
    """Extract HANA credentials from VCAP_SERVICES (CF runtime)."""
    vcap = os.environ.get("VCAP_SERVICES")
    if not vcap:
        return None
    try:
        services = json.loads(vcap)
        hana_instances = services.get("hana", [])
        if hana_instances:
            return hana_instances[0]["credentials"]
    except Exception:
        pass
    return None


def get_connection():
    """Return an hdbcli DBAPI connection. Caller must close it."""
    try:
        from hdbcli import dbapi
    except ImportError:
        raise RuntimeError("hdbcli not installed — add it to requirements.txt")

    creds = _vcap_hana_creds()
    if creds:
        conn = dbapi.connect(
            address=creds["host"],
            port=int(creds["port"]),
            user=creds["user"],
            password=creds["password"],
            encrypt=True,
            sslValidateCertificate=True,
        )
    else:
        # Local fallback: explicit env vars
        conn = dbapi.connect(
            address=os.environ["HANA_HOST"],
            port=int(os.environ.get("HANA_PORT", "443")),
            user=os.environ["HANA_USER"],
            password=os.environ["HANA_PASSWORD"],
            encrypt=True,
            sslValidateCertificate=False,
        )
    return conn


# ── Schema bootstrap ──────────────────────────────────────────────────────────

_DDL = [
    """
    CREATE TABLE IF NOT EXISTS DEMO_ORGS (
        DEMO_ID         NVARCHAR(36)   NOT NULL PRIMARY KEY,
        COMPANY_CODE    NVARCHAR(10)   NOT NULL,
        COMPANY_NAME    NVARCHAR(200)  NOT NULL,
        INDUSTRY        NVARCHAR(50),
        COUNTRY         NVARCHAR(10),
        SCENARIO        NVARCHAR(100),
        PASSWORD        NVARCHAR(100),
        CREATED_AT      TIMESTAMP      NOT NULL,
        CREATED_BY      NVARCHAR(200),
        STATUS          NVARCHAR(20)   DEFAULT 'ACTIVE'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS DEMO_ORG_EMAILS (
        DEMO_ID         NVARCHAR(36)   NOT NULL,
        USER_ID         NVARCHAR(20)   NOT NULL,
        USERNAME        NVARCHAR(100)  NOT NULL,
        EMAIL           NVARCHAR(300)  NOT NULL,
        JOB_TITLE       NVARCHAR(200),
        PAY_GRADE       NVARCHAR(10),
        PRIMARY KEY (DEMO_ID, USER_ID)
    )
    """,
]

_schema_ready = False

def ensure_schema():
    """Create tables if they don't exist. Safe to call repeatedly."""
    global _schema_ready
    if _schema_ready:
        return
    conn = get_connection()
    try:
        cur = conn.cursor()
        for ddl in _DDL:
            cur.execute(ddl.strip())
        conn.commit()
        _schema_ready = True
    finally:
        conn.close()


# ── Write helpers ─────────────────────────────────────────────────────────────

def save_demo_org(
    *,
    demo_id: str,
    company_code: str,
    company_name: str,
    industry: str,
    country: str,
    scenario: str,
    password: str,
    created_by: Optional[str],
    employees: list,
    email_prefix: str,
) -> None:
    """Persist a provisioned org and its user emails."""
    ensure_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        now = datetime.now(timezone.utc)

        cur.execute(
            """
            INSERT INTO DEMO_ORGS
              (DEMO_ID, COMPANY_CODE, COMPANY_NAME, INDUSTRY, COUNTRY,
               SCENARIO, PASSWORD, CREATED_AT, CREATED_BY, STATUS)
            VALUES (?,?,?,?,?,?,?,?,?,'ACTIVE')
            """,
            (demo_id, company_code, company_name, industry, country,
             scenario, password, now, created_by or "anonymous"),
        )

        for e in employees:
            email = f"{email_prefix}+{e['email_tag']}@sap.com"
            cur.execute(
                """
                INSERT INTO DEMO_ORG_EMAILS
                  (DEMO_ID, USER_ID, USERNAME, EMAIL, JOB_TITLE, PAY_GRADE)
                VALUES (?,?,?,?,?,?)
                """,
                (demo_id, e["userId"], e["username"], email,
                 e.get("jobTitle", ""), e.get("payGrade", "")),
            )

        conn.commit()
    finally:
        conn.close()


# ── Read helpers ──────────────────────────────────────────────────────────────

def list_demo_orgs(created_by: Optional[str] = None) -> list[dict]:
    """Return all orgs, optionally filtered to a specific creator email."""
    ensure_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        if created_by:
            cur.execute(
                "SELECT DEMO_ID, COMPANY_CODE, COMPANY_NAME, INDUSTRY, COUNTRY, "
                "SCENARIO, CREATED_AT, CREATED_BY, STATUS "
                "FROM DEMO_ORGS WHERE CREATED_BY=? ORDER BY CREATED_AT DESC",
                (created_by,),
            )
        else:
            cur.execute(
                "SELECT DEMO_ID, COMPANY_CODE, COMPANY_NAME, INDUSTRY, COUNTRY, "
                "SCENARIO, CREATED_AT, CREATED_BY, STATUS "
                "FROM DEMO_ORGS ORDER BY CREATED_AT DESC"
            )
        cols = [d[0].lower() for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def get_demo_org(demo_id: str) -> Optional[dict]:
    """Return a single org with all its user emails."""
    ensure_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT DEMO_ID, COMPANY_CODE, COMPANY_NAME, INDUSTRY, COUNTRY, "
            "SCENARIO, PASSWORD, CREATED_AT, CREATED_BY, STATUS "
            "FROM DEMO_ORGS WHERE DEMO_ID=?",
            (demo_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0].lower() for d in cur.description]
        org = dict(zip(cols, row))
        # convert datetime to ISO string for JSON serialisation
        if hasattr(org.get("created_at"), "isoformat"):
            org["created_at"] = org["created_at"].isoformat()

        cur.execute(
            "SELECT USER_ID, USERNAME, EMAIL, JOB_TITLE, PAY_GRADE "
            "FROM DEMO_ORG_EMAILS WHERE DEMO_ID=? ORDER BY USER_ID",
            (demo_id,),
        )
        ecols = [d[0].lower() for d in cur.description]
        org["users"] = [dict(zip(ecols, r)) for r in cur.fetchall()]
        return org
    finally:
        conn.close()
