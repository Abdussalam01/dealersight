# AWS in DealerSight (AWS Builder mini challenge)

Every AWS service listed here does real work at runtime. Nothing is listed that the project does not
actually call, and nothing planned is described as implemented.

## Services used

| Service | Region | What it does in DealerSight | Where in the code |
|---|---|---|---|
| **Amazon Bedrock** (Converse API, `amazon.nova-pro-v1:0`) | us-east-1 | Explains a computed metric packet for the three fixed analyst questions. It receives facts derived from the database and performs no arithmetic. | `backend/analyst.py` → `ask()` → `client.converse(...)` |
| **Amazon SQS** (standard queue + dead-letter queue) | us-east-1 | Carries each Ring event from the poller to ingestion when `EVENT_PIPELINE=sqs`, so polling and processing are separated and a failed ingest is retried rather than lost. **Opt-in: `local` is the default**, and the whole project runs without it. | `backend/pipeline.py`, `backend/ring_poller.py` → `_deliver()` |
| **AWS IAM** | global | A dedicated project user (`dealersight-dev`) whose only permissions are invoking Bedrock foundation models and US cross-region inference profiles, reading the model catalogue, and — if the queue is enabled — send/receive/delete on that one queue | policy below; credentials come from the standard AWS credential chain |

Not used, and therefore not in the architecture diagram: Lambda, EventBridge, Step Functions, S3, RDS,
Bedrock Agents, Bedrock Knowledge Bases. The database is PostgreSQL running locally in Docker.

## The multi-step workflow

```
fixed question id
   -> packet builder in application code (SQL + Python)        backend/packets.py
   -> completeness check; if a metric is missing the model is never called
   -> Amazon Bedrock Converse, temperature 0, ~10 s deadline   backend/analyst.py
   -> validation of every claim against the packet
   -> answer shown beside the exact numbers it used            frontend/index.html
   -> full call logged (packet, answer, model, tokens, stop reason, latency, errors)
```

DealerSight calculates; Bedrock explains. The model answers in JSON: a summary with no digits, a list of
claims that each name a metric id from the packet, and things to investigate. Each claim is checked for
metric, value, unit and direction, so a correct number attached to the wrong metric, period or dealership
is rejected. The numbers on screen are read from the packet, never from the model.

## Setup for a reviewer

1. **Create an IAM user** (or role) for the project and generate an access key.
2. **Attach this inline policy.** It grants Bedrock invocation and model-catalogue reads, and nothing
   else — no other AWS service, and no write access to anything:

```json
{
	"Version": "2012-10-17",
	"Statement": [
		{
			"Sid": "DealerSightBedrockModelInformation",
			"Effect": "Allow",
			"Action": [
				"bedrock:GetFoundationModel",
				"bedrock:ListFoundationModels",
				"bedrock:GetInferenceProfile",
				"bedrock:ListInferenceProfiles"
			],
			"Resource": "*"
		},
		{
			"Sid": "DealerSightInvokeUSModelsAndProfiles",
			"Effect": "Allow",
			"Action": [
				"bedrock:InvokeModel",
				"bedrock:InvokeModelWithResponseStream"
			],
			"Resource": [
				"arn:aws:bedrock:us-*::foundation-model/*",
				"arn:aws:bedrock:us-east-1:*:inference-profile/us.*"
			]
		}
	]
}
```

   Two details in that policy are not obvious, and a narrower one fails at runtime:

   * **`arn:aws:bedrock:us-*::foundation-model/*`**, not just `us-east-1`. Several Amazon models are
     served through *cross-region* inference, where a request made in us-east-1 may be fulfilled in
     another US region. A policy pinned to a single region is denied when that happens.
   * **`arn:aws:bedrock:us-east-1:*:inference-profile/us.*`**. Model ids prefixed `us.` are inference
     profiles rather than foundation models, and they are authorised as a separate resource type.
     Invoking one with only `foundation-model/*` permission is denied.

   `bedrock:Converse` does not need to be listed: the Converse API authorises against
   `bedrock:InvokeModel`.

3. **Enable model access** in the Bedrock console for `amazon.nova-pro-v1:0` (Amazon models are usually
   available immediately; Anthropic models additionally require a use-case details form).
4. **Configure credentials**: `aws configure --profile dealersight`.
5. **Set the environment variables** in `.env` (see `.env.example`):

```
AWS_PROFILE=dealersight
AWS_REGION=us-east-1
BEDROCK_MODEL_ID=amazon.nova-pro-v1:0
```

6. Start the app and open the **AI analyst** tab. The audit log at the bottom of that tab shows every
   call with its model id, latency, token counts and stop reason.

A one-line check that credentials and model access work:

```powershell
python scripts\bedrock_hello.py
```

## The event pipeline (optional)

`EVENT_PIPELINE=local`, the default, has the Ring poller ingest each event directly. It needs no AWS
account and is the mode a reviewer should use.

`EVENT_PIPELINE=sqs` puts Amazon SQS in the live event path:

```
Ring poller  ->  SQS queue  ->  DealerSight worker  ->  de-duplication and zone rules  ->  PostgreSQL
                    |
                    +-- dead-letter queue (after 5 failed receives)
```

Three properties make at-least-once delivery safe here, and each has a test in `tests/test_pipeline.py`:

| Property | Why it holds |
|---|---|
| A duplicate delivery changes no metric | Ingestion is keyed on the Ring event id under a unique constraint, so a second delivery is recorded as `duplicate` |
| A crash mid-ingest loses nothing | The message is deleted **only after** the database transaction commits; otherwise it becomes visible again after the visibility timeout |
| One bad event cannot block the rest | A message that keeps failing is moved to the dead-letter queue by the redrive policy, and DealerSight reports the dead-letter depth in `/api/status` |

An unreadable message (malformed JSON) is deleted rather than retried, because redelivery cannot fix it;
it is logged instead.

### Setting up the queue

1. **Create the dead-letter queue first**: SQS → Create queue → **Standard** → name `dealersight-events-dlq`.
   Defaults are fine.
2. **Create the main queue**: Standard → name `dealersight-events` → set **Visibility timeout** to
   `60` seconds → under **Dead-letter queue**, enable it, choose `dealersight-events-dlq`, and set
   **Maximum receives** to `5`.
3. Copy both queue URLs.
4. **Add this statement** to the policy above, with your own queue ARNs:

```json
{
  "Sid": "DealerSightEventQueue",
  "Effect": "Allow",
  "Action": [
    "sqs:SendMessage",
    "sqs:ReceiveMessage",
    "sqs:DeleteMessage",
    "sqs:GetQueueAttributes"
  ],
  "Resource": "arn:aws:sqs:us-east-1:<account-id>:dealersight-events"
},
{
  "Sid": "DealerSightDeadLetterQueueMonitoring",
  "Effect": "Allow",
  "Action": "sqs:GetQueueAttributes",
  "Resource": "arn:aws:sqs:us-east-1:<account-id>:dealersight-events-dlq"
}
```

DealerSight never sends to, receives from or deletes from the dead-letter queue — it only reads its
depth for the status panel — so the second statement is deliberately narrower than the first. SQS itself
moves messages there under the redrive policy; that does not require any permission of ours.

Set the dead-letter queue's **message retention period longer than the main queue's** — 14 days against
the default 4 — so a message that fails is still there to inspect after a weekend. AWS recommends the
dead-letter queue retain messages longer than its source, because retention is counted from the moment
the message first entered the *original* queue, not from when it was moved.

5. **Set the environment variables** and restart:

```
EVENT_PIPELINE=sqs
SQS_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/<account-id>/dealersight-events
SQS_DLQ_URL=https://sqs.us-east-1.amazonaws.com/<account-id>/dealersight-events-dlq
```

If `EVENT_PIPELINE=sqs` is set but `SQS_QUEUE_URL` is empty, DealerSight stays in `local` mode rather
than silently dropping the live event path.

### Checking the queue without a Ring token

```powershell
python scripts\queue_check.py
```

Publishes one synthetic Ring-shaped event, consumes it, and reports what SQS did at each step. The event
names a device that does not exist, so ingestion rejects it as unknown and the funnel is left untouched —
this checks the transport, not the funnel. Stop the application first, or its own worker will consume the
message before this script sees it.

## Model choice

Measured by `scripts/benchmark_models.py`, which asks all three questions (including one whose premise the
data does not support) several times per model and runs each answer through the same validator the
application uses. Full per-call results, with the prompt and packet schema versions, are in
`docs/evidence/model-benchmark.json`.

| Model | Answers accepted by the validator | Median latency |
|---|---|---|
| `amazon.nova-pro-v1:0` | 12/12 | ~2.0 s |
| `amazon.nova-micro-v1:0` | 11/12 | ~0.8 s |
| `us.amazon.nova-2-lite-v1:0` | 6/12 | ~1.1 s |

This measures **validator acceptance**: the answer parsed, named real metric ids with the right values,
units and directions, carried the required no-causation statement and did not contradict itself. It is not
a judgement of how insightful the prose is; the sampled summaries in the evidence file support that.

Nova Micro is a cheaper alternative and works: set `BEDROCK_MODEL_ID=amazon.nova-micro-v1:0`.

## Cost profile

One analyst question costs roughly 3,300 input tokens and 175 output tokens. The analyst runs only when
someone presses one of the three question buttons, so a full demonstration is a handful of calls.
Current pricing per model is on the Amazon Bedrock pricing page.

## Failure handling

| Situation | What DealerSight does |
|---|---|
| Required metric missing | Returns "insufficient data" and never calls Bedrock |
| Throttling, 5xx, model timeout | One retry with jittered backoff inside a ~10 s deadline, then a deterministic answer built from the packet |
| Access denied, unknown model, malformed request | No retry; deterministic fallback immediately |
| `stopReason` other than `end_turn` / `stop_sequence` (max tokens, content filter, guardrail) | Deterministic fallback |
| Model answer fails claim validation | Answer rejected, deterministic fallback shown, rejected text kept in the audit log |
| Queued event fails to ingest (`sqs` mode) | The message is not deleted, so SQS redelivers it after the visibility timeout; after 5 receives the redrive policy moves it to the dead-letter queue |
| Worker unavailable (`sqs` mode) | Published events accumulate in the queue until a worker consumes them; nothing is lost |
| Publishing to the queue fails (`sqs` mode) | The poll fails and is retried on the next interval. Events are re-fetched from Ring Event History, so anything still inside the 30-minute overlap window is recovered; delivery is not guaranteed for an outage longer than that window |

Errors are sanitized before they are stored or displayed: ARNs, access key ids, 12-digit account ids and
request ids are replaced with placeholders.
