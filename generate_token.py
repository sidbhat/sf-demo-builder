#!/usr/bin/env python3
"""
Local dev token generator — issues a signed JWT that mimics a SAP XSUAA token.
Use this to test the MCP server's auth layer locally without a real SAP IDP.

NOT for production. The HS256 secret is only for local smoke-testing.

Usage:
  python3 generate_token.py --email siddhartha.bhattacharya+se.ceo@sap.com
  python3 generate_token.py --email your.name+rol.eng@sap.com --secret mysecret

Then pass the token in requests:
  curl -H "Authorization: Bearer <token>" http://localhost:8000/mcp
"""
import argparse
import json
import time
import sys

try:
    import jwt
except ImportError:
    print("PyJWT not installed. Run: pip install PyJWT")
    sys.exit(1)

DEFAULT_SECRET = "dev-local-secret-not-for-production"

def generate(email: str, secret: str, ttl_seconds: int = 3600) -> str:
    now = int(time.time())
    payload = {
        "iss": "https://accounts.sap.com",
        "sub": email.split("@")[0],
        "aud": "sf-demo-builder",
        "email": email,
        "user_name": email.split("@")[0],
        "iat": now,
        "exp": now + ttl_seconds,
    }
    token = jwt.encode(payload, secret, algorithm="HS256")
    return token

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate a local dev JWT")
    parser.add_argument("--email",  required=True, help="SAP email (use +alias for persona)")
    parser.add_argument("--secret", default=DEFAULT_SECRET, help="HS256 signing secret")
    parser.add_argument("--ttl",    type=int, default=3600, help="Token TTL in seconds")
    parser.add_argument("--json",   action="store_true", help="Output as JSON")
    args = parser.parse_args()

    token = generate(args.email, args.secret, args.ttl)

    if args.json:
        # Decode without verification to show claims
        claims = jwt.decode(token, options={"verify_signature": False})
        print(json.dumps({"token": token, "claims": claims}, indent=2))
    else:
        print(token)
        print(f"\nEmail:  {args.email}")
        local = args.email.split("@")[0]
        alias = local.split("+")[1] if "+" in local else None
        print(f"Alias:  {alias or '(none — no + in email)'}")
        print(f"SF user: {alias + '@SFSALES011375' if alias else 'n/a'}")
        print(f"\nTest with:")
        print(f'  curl -H "Authorization: Bearer {token[:40]}..." http://localhost:8000/mcp')
