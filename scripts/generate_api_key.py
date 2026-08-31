"""
Generate API keys.

    python scripts/generate_api_key.py            one key
    python scripts/generate_api_key.py 3          three keys

Use secrets.token_urlsafe, not random.choice or a uuid4 string. uuid4 is fine
for identifiers and wrong for credentials -- it is not generated from a
cryptographic source on every platform. token_urlsafe reads from the OS CSPRNG.

Issue a SEPARATE key per consumer (one for Base44, one for your scripts, one
for testing). API_KEYS takes a comma-separated list, so you can revoke one
without breaking the others.
"""
import secrets
import sys

n = int(sys.argv[1]) if len(sys.argv) > 1 else 1
keys = [f"apr_{secrets.token_urlsafe(32)}" for _ in range(n)]

for k in keys:
    print(k)

print("\nFor local Docker (PowerShell):")
print(f'  $env:API_KEYS = "{",".join(keys)}"')
print("\nFor AWS Secrets Manager:")
print(f'  aws secretsmanager create-secret --name appraisal-api-keys --secret-string "{",".join(keys)}"')
