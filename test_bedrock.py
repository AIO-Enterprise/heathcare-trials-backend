"""Quick Bedrock connection test — run from the backend/ directory."""

import os
import sys
import time

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

aws_key = os.getenv("AWS_ACCESS_KEY_ID")
aws_secret = os.getenv("AWS_SECRET_ACCESS_KEY")
aws_region = os.getenv("AWS_REGION", "us-east-1")
bedrock_model = os.getenv("AWS_GENERIC_MODEL", "")

print(f"AWS_REGION        : {aws_region}")
print(f"AWS_GENERIC_MODEL : {bedrock_model or 'NOT SET'}")
print(f"KEY present       : {'yes' if aws_key else 'NO — missing (boto chain/IAM role may still work)'})")
print()

if not bedrock_model:
    print("AWS_GENERIC_MODEL is empty — nothing to test.")
    sys.exit(0)

try:
    from anthropic import AnthropicBedrock
except ImportError:
    print("anthropic[bedrock] not installed — run: pip install 'anthropic[bedrock]'")
    sys.exit(1)

print("Connecting to Bedrock …")
kwargs = {"aws_region": aws_region}
if aws_key:
    kwargs["aws_access_key"] = aws_key
if aws_secret:
    kwargs["aws_secret_key"] = aws_secret
client = AnthropicBedrock(**kwargs)

for attempt in range(3):
    try:
        response = client.messages.create(
            model=bedrock_model,
            max_tokens=64,
            messages=[{"role": "user", "content": "Reply with exactly: BEDROCK_OK"}],
        )
        reply = response.content[0].text.strip()
        print(f"Response     : {reply}")
        print()
        print(
            "SUCCESS: Bedrock connection working."
            if "BEDROCK_OK" in reply
            else "SUCCESS: Got a response (connection works)."
        )
        sys.exit(0)
    except Exception as e:
        code = getattr(e, "status_code", "?")
        print(f"Attempt {attempt + 1}/3 failed [{code}]: {e}")
        if attempt < 2:
            print("Retrying in 3s...")
            time.sleep(3)

print("FAILED: All attempts exhausted.")
sys.exit(1)
