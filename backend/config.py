"""Settings loaded from .env (see .env.example)."""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://dealersight:dealersight@localhost:5433/dealersight")
RING_API_BASE = os.getenv("RING_API_BASE", "https://api.amazonvision.com")
RING_POLL_SECONDS = float(os.getenv("RING_POLL_SECONDS", "5"))
DEVICE_MODE = os.getenv("DEVICE_MODE", "demo")  # demo | production
ARM_MINUTES = float(os.getenv("ARM_MINUTES", "10"))
FUTURE_TOLERANCE_SECONDS = 300

# AWS / Bedrock (the analyst). Credentials come from the normal AWS credential chain.
AWS_PROFILE = os.getenv("AWS_PROFILE", "")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "")


def ring_access_token():
    """Read at call time so a refreshed token in .env is picked up after reload."""
    load_dotenv(ROOT / ".env", override=True)
    return os.getenv("RING_ACCESS_TOKEN", "")
