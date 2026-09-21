# Product Feedback (draft, updated as we build)

Answer the five questions for every Ring, AWS, or other developer tool used.

## Ring Partner API (api.amazonvision.com)
1. **Used for:**
2. **What worked well:**
3. **Needs improvement:**
4. **Onboarding (zero to working example):**
5. **Would build with it again? Why:**

## Ring Developer Playground
1. **Used for:**
2. **What worked well:**
3. **Needs improvement:**
4. **Onboarding:**
5. **Would build with it again? Why:**

## ring-api-helloworld sample
1. **Used for:** Phase 0 exploration of the API before writing DealerSight's own client
2. **What worked well:**
3. **Needs improvement:**
4. **Onboarding:**
5. **Would build with it again? Why:**

## Amazon Bedrock (Converse API via boto3)
1. **Used for:** Grounded analyst that explains computed metric packets (Phase 4). Phase 0 spike: `scripts/bedrock_hello.py`.
2. **What worked well:** First Converse call worked on 2026-09-21 with `amazon.nova-micro-v1:0` in `us-east-1`: ~857 ms latency, 32 total tokens. The Converse API returns token usage per call, which is useful for logging.
3. **Needs improvement:** Even a neutral prompt ("say hello in one sentence") produced marketing-style text ("revolutionizing", "cutting-edge"), so the Phase 4 prompt must strictly constrain tone and grounding.
4. **Onboarding:**
5. **Would build with it again? Why:**

## AWS services used (AWS Builder)
| Service | Region | Purpose in DealerSight | Where in code |
|---|---|---|---|
| Amazon Bedrock (`amazon.nova-micro-v1:0`, Converse API) | us-east-1 | Explains computed funnel metrics with visible evidence | `scripts/bedrock_hello.py` (spike); analyst module in Phase 4 |
