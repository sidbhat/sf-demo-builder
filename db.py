"""
HANA persistence layer for SF Demo Builder.

Tables (created on first connect if not present):
  DEMO_ORGS        — one row per provisioned demo org
  DEMO_ORG_EMAILS  — one row per user email in that org
  DEMO_API_KEYS    — hashed API keys for A2A callers (one key = one user identity)

Connection comes from VCAP_SERVICES (CF) or env vars (local).
"""

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

# ── Connection factory ────────────────────────────────────────────────────────

def _vcap_hana_creds() -> Optional[dict]:
    """Extract HANA credentials from VCAP_SERVICES (CF runtime).

    HDI container bindings (plan=hdi-shared) appear under key 'hana' and carry
    user/password directly. Raw hana-cloud bindings (plan=hana) carry UAA only.
    Prefer whichever has user+password.
    """
    vcap = os.environ.get("VCAP_SERVICES")
    if not vcap:
        return None
    try:
        services = json.loads(vcap)
        for key in ("hana", "hana-cloud", "hanatrial"):
            for inst in services.get(key, []):
                creds = inst.get("credentials", {})
                if "user" in creds and "password" in creds:
                    return creds
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

_CREATE_DDL = [
    """
    CREATE TABLE DEMO_ORGS (
        DEMO_ID         NVARCHAR(36)   NOT NULL PRIMARY KEY,
        COMPANY_CODE    NVARCHAR(10)   NOT NULL,
        COMPANY_NAME    NVARCHAR(200)  NOT NULL,
        INDUSTRY        NVARCHAR(50),
        COUNTRY         NVARCHAR(10),
        SCENARIO        NVARCHAR(100),
        PASSWORD        NVARCHAR(100),
        CREATED_AT      TIMESTAMP      NOT NULL,
        CREATED_BY      NVARCHAR(200),
        STATUS          NVARCHAR(20)   DEFAULT 'ACTIVE',
        PLAN_JSON       NCLOB
    )
    """,
    """
    CREATE TABLE DEMO_ORG_EMAILS (
        DEMO_ID         NVARCHAR(36)   NOT NULL,
        USER_ID         NVARCHAR(20)   NOT NULL,
        USERNAME        NVARCHAR(100)  NOT NULL,
        EMAIL           NVARCHAR(300)  NOT NULL,
        JOB_TITLE       NVARCHAR(200),
        PAY_GRADE       NVARCHAR(10),
        DEPARTMENT      NVARCHAR(50),
        GOALS_JSON      NCLOB,
        PRIMARY KEY (DEMO_ID, USER_ID)
    )
    """,
    """
    CREATE TABLE DEMO_API_KEYS (
        KEY_HASH        NVARCHAR(64)   NOT NULL PRIMARY KEY,
        OWNER_EMAIL     NVARCHAR(200)  NOT NULL,
        LABEL           NVARCHAR(100),
        CREATED_AT      TIMESTAMP      NOT NULL,
        LAST_USED_AT    TIMESTAMP,
        ACTIVE          BOOLEAN        DEFAULT TRUE
    )
    """,
    """
    CREATE TABLE DEMO_SF_CONFIGS (
        OWNER_EMAIL     NVARCHAR(200)  NOT NULL PRIMARY KEY,
        API_BASE_URL    NVARCHAR(300)  NOT NULL,
        ADMIN_USER      NVARCHAR(200)  NOT NULL,
        ADMIN_PASS_B64  NVARCHAR(500)  NOT NULL,
        LOGIN_URL       NVARCHAR(500)  NOT NULL,
        IAS_BASE_URL    NVARCHAR(300),
        IAS_CLIENT_ID   NVARCHAR(200),
        IAS_SECRET_B64  NVARCHAR(500),
        CREATED_AT      TIMESTAMP      NOT NULL,
        UPDATED_AT      TIMESTAMP      NOT NULL
    )
    """,
]

# Best-effort migrations — all silently ignored if column/table already exists
_MIGRATION_DDL = [
    "ALTER TABLE DEMO_ORGS ADD (PLAN_JSON NCLOB)",
    "ALTER TABLE DEMO_ORG_EMAILS ADD (DEPARTMENT NVARCHAR(50))",
    "ALTER TABLE DEMO_ORG_EMAILS ADD (GOALS_JSON NCLOB)",
]

_schema_ready = False

def ensure_schema():
    """Create tables and run column migrations. All statements are best-effort."""
    global _schema_ready
    if _schema_ready:
        return
    conn = get_connection()
    try:
        cur = conn.cursor()
        for ddl in _CREATE_DDL + _MIGRATION_DDL:
            try:
                cur.execute(ddl.strip())
            except Exception:
                pass  # table/column already exists — fine
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
    plan_json: Optional[str] = None,
) -> None:
    """Persist a provisioned org, its user emails, and the full plan JSON."""
    ensure_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        now = datetime.now(timezone.utc)

        cur.execute(
            """
            INSERT INTO DEMO_ORGS
              (DEMO_ID, COMPANY_CODE, COMPANY_NAME, INDUSTRY, COUNTRY,
               SCENARIO, PASSWORD, CREATED_AT, CREATED_BY, STATUS, PLAN_JSON)
            VALUES (?,?,?,?,?,?,?,?,?,'ACTIVE',?)
            """,
            (demo_id, company_code, company_name, industry, country,
             scenario, password, now, created_by or "anonymous", plan_json),
        )

        for e in employees:
            email = f"{email_prefix}+{e['email_tag']}@sap.com"
            goals_str = json.dumps(e.get("goals") or {})
            cur.execute(
                """
                INSERT INTO DEMO_ORG_EMAILS
                  (DEMO_ID, USER_ID, USERNAME, EMAIL, JOB_TITLE, PAY_GRADE, DEPARTMENT, GOALS_JSON)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (demo_id, e["userId"], e["username"], email,
                 e.get("jobTitle", ""), e.get("payGrade", ""),
                 e.get("dept_key", ""), goals_str),
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
        if hasattr(org.get("created_at"), "isoformat"):
            org["created_at"] = org["created_at"].isoformat()

        cur.execute(
            "SELECT USER_ID, USERNAME, EMAIL, JOB_TITLE, PAY_GRADE, DEPARTMENT, GOALS_JSON "
            "FROM DEMO_ORG_EMAILS WHERE DEMO_ID=? ORDER BY USER_ID",
            (demo_id,),
        )
        ecols = [d[0].lower() for d in cur.description]
        users = []
        for r in cur.fetchall():
            u = dict(zip(ecols, r))
            if u.get("goals_json"):
                try:
                    u["goals"] = json.loads(u["goals_json"])
                except Exception:
                    u["goals"] = {}
            del u["goals_json"]
            users.append(u)
        org["users"] = users
        return org
    finally:
        conn.close()


def get_org_created_by(demo_id: str) -> Optional[str]:
    """Return the CREATED_BY email for an org, or None if not found."""
    ensure_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT CREATED_BY FROM DEMO_ORGS WHERE DEMO_ID=?", (demo_id,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def get_org_plan_json(demo_id: str) -> Optional[str]:
    """Return the raw PLAN_JSON stored at provision time, or None."""
    ensure_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT PLAN_JSON FROM DEMO_ORGS WHERE DEMO_ID=?", (demo_id,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def delete_demo_org(demo_id: str) -> None:
    """Remove a demo org and all its email rows from HANA. No SF changes — caller handles that."""
    ensure_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM DEMO_ORG_EMAILS WHERE DEMO_ID=?", (demo_id,))
        cur.execute("DELETE FROM DEMO_ORGS WHERE DEMO_ID=?", (demo_id,))
        conn.commit()
    finally:
        conn.close()


# ── API-key helpers ───────────────────────────────────────────────────────────

def save_api_key(key_hash: str, owner_email: str, label: str) -> None:
    """Store a new API key (hash only). Raises if label already exists for this owner."""
    ensure_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        now = datetime.now(timezone.utc)
        cur.execute(
            "INSERT INTO DEMO_API_KEYS (KEY_HASH, OWNER_EMAIL, LABEL, CREATED_AT, ACTIVE) "
            "VALUES (?, ?, ?, ?, TRUE)",
            (key_hash, owner_email, label, now),
        )
        conn.commit()
    finally:
        conn.close()


def lookup_api_key(key_hash: str) -> Optional[str]:
    """Return owner_email if the key is active, else None."""
    ensure_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT OWNER_EMAIL FROM DEMO_API_KEYS WHERE KEY_HASH=? AND ACTIVE=TRUE",
            (key_hash,),
        )
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def touch_api_key(key_hash: str) -> None:
    """Update LAST_USED_AT. Silently swallows errors — non-critical."""
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE DEMO_API_KEYS SET LAST_USED_AT=? WHERE KEY_HASH=?",
                (datetime.now(timezone.utc), key_hash),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def revoke_api_key(key_hash: str, owner_email: str) -> bool:
    """Deactivate a key. Returns True if a row was updated, False if not found/wrong owner."""
    ensure_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE DEMO_API_KEYS SET ACTIVE=FALSE "
            "WHERE KEY_HASH=? AND OWNER_EMAIL=? AND ACTIVE=TRUE",
            (key_hash, owner_email),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_api_keys(owner_email: str) -> list[dict]:
    """Return all keys for an owner — label, created_at, last_used_at, active. Never key_hash."""
    ensure_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT LABEL, CREATED_AT, LAST_USED_AT, ACTIVE "
            "FROM DEMO_API_KEYS WHERE OWNER_EMAIL=? ORDER BY CREATED_AT DESC",
            (owner_email,),
        )
        cols = [d[0].lower() for d in cur.description]
        rows = []
        for r in cur.fetchall():
            row = dict(zip(cols, r))
            for ts_field in ("created_at", "last_used_at"):
                if hasattr(row.get(ts_field), "isoformat"):
                    row[ts_field] = row[ts_field].isoformat()
            rows.append(row)
        return rows
    finally:
        conn.close()


def revoke_api_key_by_label(label: str, owner_email: str) -> bool:
    """Deactivate the key matching owner + label. Returns True if found and deactivated."""
    ensure_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        # First get the hash so we can call the hash-based revoke
        cur.execute(
            "SELECT KEY_HASH FROM DEMO_API_KEYS "
            "WHERE LABEL=? AND OWNER_EMAIL=? AND ACTIVE=TRUE",
            (label, owner_email),
        )
        row = cur.fetchone()
        if not row:
            return False
        cur.execute(
            "UPDATE DEMO_API_KEYS SET ACTIVE=FALSE WHERE KEY_HASH=?",
            (row[0],),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ── Per-user SF instance configuration ────────────────────────────────────────

def save_sf_config(
    *,
    owner_email: str,
    api_base_url: str,
    admin_user: str,
    admin_pass: str,
    login_url: str,
    ias_base_url: Optional[str],
    ias_client_id: Optional[str],
    ias_client_secret: Optional[str],
) -> None:
    """Upsert a per-user SF instance configuration."""
    import base64 as _b64
    from datetime import datetime
    ensure_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        now = datetime.utcnow()
        pass_b64 = _b64.b64encode(admin_pass.encode()).decode()
        secret_b64 = _b64.b64encode(ias_client_secret.encode()).decode() if ias_client_secret else None
        cur.execute(
            "SELECT OWNER_EMAIL FROM DEMO_SF_CONFIGS WHERE OWNER_EMAIL=?",
            (owner_email,),
        )
        if cur.fetchone():
            cur.execute(
                """UPDATE DEMO_SF_CONFIGS SET
                    API_BASE_URL=?, ADMIN_USER=?, ADMIN_PASS_B64=?, LOGIN_URL=?,
                    IAS_BASE_URL=?, IAS_CLIENT_ID=?, IAS_SECRET_B64=?, UPDATED_AT=?
                WHERE OWNER_EMAIL=?""",
                (api_base_url, admin_user, pass_b64, login_url,
                 ias_base_url, ias_client_id, secret_b64, now, owner_email),
            )
        else:
            cur.execute(
                """INSERT INTO DEMO_SF_CONFIGS
                    (OWNER_EMAIL, API_BASE_URL, ADMIN_USER, ADMIN_PASS_B64, LOGIN_URL,
                     IAS_BASE_URL, IAS_CLIENT_ID, IAS_SECRET_B64, CREATED_AT, UPDATED_AT)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (owner_email, api_base_url, admin_user, pass_b64, login_url,
                 ias_base_url, ias_client_id, secret_b64, now, now),
            )
        conn.commit()
    finally:
        conn.close()


def get_sf_config(owner_email: str) -> Optional[dict]:
    """Return the SF config for a user, or None if not set."""
    import base64 as _b64
    ensure_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT API_BASE_URL, ADMIN_USER, ADMIN_PASS_B64, LOGIN_URL, "
            "IAS_BASE_URL, IAS_CLIENT_ID, IAS_SECRET_B64, CREATED_AT, UPDATED_AT "
            "FROM DEMO_SF_CONFIGS WHERE OWNER_EMAIL=?",
            (owner_email,),
        )
        row = cur.fetchone()
        if not row:
            return None
        secret_b64 = row[6]
        return {
            "api_base_url":      row[0],
            "admin_user":        row[1],
            "admin_pass":        _b64.b64decode(row[2]).decode(),
            "login_url":         row[3],
            "ias_base_url":      row[4],
            "ias_client_id":     row[5],
            "ias_client_secret": _b64.b64decode(secret_b64).decode() if secret_b64 else None,
            "created_at":        str(row[7]),
            "updated_at":        str(row[8]),
        }
    finally:
        conn.close()


def delete_sf_config(owner_email: str) -> bool:
    """Remove a user's custom SF config. Returns True if a row was deleted."""
    ensure_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM DEMO_SF_CONFIGS WHERE OWNER_EMAIL=?", (owner_email,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
