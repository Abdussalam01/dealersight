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

# Event delivery between the Ring poller and ingestion.
#   local (default) - the poller ingests directly; needs no AWS account
#   sqs             - the poller publishes to Amazon SQS and a worker ingests
EVENT_PIPELINE = os.getenv("EVENT_PIPELINE", "local").strip().lower()
SQS_QUEUE_URL = os.getenv("SQS_QUEUE_URL", "")
SQS_DLQ_URL = os.getenv("SQS_DLQ_URL", "")
SQS_WAIT_SECONDS = int(os.getenv("SQS_WAIT_SECONDS", "20"))   # long poll; 20 is the SQS maximum
SQS_BATCH = int(os.getenv("SQS_BATCH", "10"))                 # messages per receive, 10 is the maximum


def ring_access_token():
    """Read at call time so a refreshed token in .env is picked up after reload."""
    load_dotenv(ROOT / ".env", override=True)
    return os.getenv("RING_ACCESS_TOKEN", "")
