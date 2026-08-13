"""SF instance configuration tools."""

import json
import base64
import urllib.request
from typing import Optional

import db as _db
from auth import _build_sf_config, _extract_caller_email, _detect_ias_from_login_url, CTX


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


def register(mcp):
    """Register config tools with mcp instance."""
    mcp.tool()(configure_sf_instance)
    mcp.tool()(get_sf_instance_config)
    mcp.tool()(reset_sf_instance_config)
