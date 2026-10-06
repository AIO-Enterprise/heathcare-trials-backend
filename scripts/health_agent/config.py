"""Static config for the weekly health agent."""

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")

BACKEND_URL = os.getenv("HEALTH_BACKEND_URL", "http://localhost:8001")
FRONTEND_URL = os.getenv("HEALTH_FRONTEND_URL", "http://localhost:5173")
API = f"{BACKEND_URL}/api"

ADMIN_EMAIL = os.getenv("HEALTH_ADMIN_EMAIL", "admin@medicare.com")
ADMIN_PASSWORD = os.getenv("HEALTH_ADMIN_PASSWORD", "medicare123")

REPORTS_DIR = REPO_ROOT / "reports"
BACKEND_DIR = REPO_ROOT / "backend"

PROBE_TIMEOUT = 30

LAYERS = ["infra", "providers", "routes", "services", "e2e"]
