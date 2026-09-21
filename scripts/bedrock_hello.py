#!/usr/bin/env python3
"""Phase 0 spike: one Amazon Bedrock Converse call, printing the reply and latency."""

import os
import time
from pathlib import Path

import boto3
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

session = boto3.Session(profile_name=os.getenv("AWS_PROFILE") or None, region_name=os.getenv("AWS_REGION", "us-east-1"))
client = session.client("bedrock-runtime")
model_id = os.environ["BEDROCK_MODEL_ID"]

start = time.perf_counter()
response = client.converse(
    modelId=model_id,
    messages=[{"role": "user", "content": [{"text": "Say hello to DealerSight in one sentence."}]}],
    inferenceConfig={"maxTokens": 100},
)
latency_ms = (time.perf_counter() - start) * 1000

print(response["output"]["message"]["content"][0]["text"])
print(f"\nmodel={model_id}  region={session.region_name}  latency={latency_ms:.0f} ms  usage={response['usage']}")
