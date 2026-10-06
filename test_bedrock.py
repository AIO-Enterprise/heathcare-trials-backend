"""Quick Bedrock connection test — run from the backend/ directory."""

import os
import sys
import time

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

use_bedrock = os.getenv("USE_BEDROCK", "false").lower() == "true"
aws_key = os.getenv("AWS_ACCESS_KEY_ID")
aws_secret = os.getenv("AWS_SECRET_ACCESS_KEY")
aws_region = os.getenv("AWS_REGION", "us-east-1")
bedrock_model = os.getenv("BEDROCK_MODEL", "anthropic.claude-sonnet-4-6")

print(f"USE_BEDROCK  : {use_bedrock}")
print(f"AWS_REGION   : {aws_region}")
print(f"BEDROCK_MODEL: {bedrock_model}")
print(f"KEY present  : {'yes' if aws_key else 'NO — missing'}")
print()

if not use_bedrock:
    print("USE_BEDROCK is false — nothing to test.")
    sys.exit(0)
if not aws_key or not aws_secret:
    print("AWS credentials missing in .env")
    sys.exit(1)

try:
    from anthropic import AnthropicBedrock
except ImportError:
    print("anthropic[bedrock] not installed — run: pip install 'anthropic[bedrock]'")
    sys.exit(1)

print("Connecting to Bedrock …")
client = AnthropicBedrock(
    aws_access_key=aws_key,
    aws_secret_key=aws_secret,
    aws_region=aws_region,
)

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
