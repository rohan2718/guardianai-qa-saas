"""
check_env.py — Run this from D:\\website_qa_tool to diagnose auth config
Usage: python check_env.py
"""
from pathlib import Path
from dotenv import load_dotenv
import os

env_path = Path(__file__).resolve().parent / ".env"
print(f"Loading .env from: {env_path}")
print(f"File exists: {env_path.exists()}")

load_dotenv(dotenv_path=env_path, override=True)

keys = [
    "CRAWLER_USERNAME",
    "CRAWLER_PASSWORD",
    "CRAWLER_LOGIN_URL",
    "CRAWLER_USERNAME_FIELD",
    "CRAWLER_PASSWORD_FIELD",
    "CRAWLER_SUBMIT",
    "CRAWLER_SUCCESS_URL",
    "CRAWLER_SKIP_URLS",
]

print("\n── Auth env vars ──────────────────────────")
all_ok = True
for k in keys:
    v = os.environ.get(k, "")
    status = "✓" if v else "✗ MISSING"
    if not v:
        all_ok = False
    # Mask password
    display = "***" if "PASSWORD" in k and v else v
    print(f"  {status}  {k} = {display!r}")

print()
if all_ok:
    print("✓ All auth vars present — crawler should attempt login.")
else:
    print("✗ Some vars missing — check your .env file encoding and line endings.")
    print("  Tip: open .env in Notepad++ and verify it's saved as UTF-8 without BOM.")
    print("       Make sure there are NO spaces around the = sign.")