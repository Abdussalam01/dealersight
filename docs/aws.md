# AWS in DealerSight (AWS Builder mini challenge)

Every AWS service listed here does real work at runtime. Nothing is listed that the project does not
actually call, and nothing planned is described as implemented.

## Services used

| Service | Region | What it does in DealerSight | Where in the code |
|---|---|---|---|
| **Amazon Bedrock** (Converse API, `amazon.nova-pro-v1:0`) | us-east-1 | Explains a computed metric packet for the three fixed analyst questions. It receives facts derived from the database and performs no arithmetic. | `backend/analyst.py` → `ask()` → `client.converse(...)` |
| **AWS IAM** | global | A dedicated project user (`dealersight-dev`) whose only permission is invoking Bedrock models in us-east-1 | policy below; credentials come from the standard AWS credential chain |

Not used, and therefore not in the architecture diagram: Lambda, SQS, EventBridge, Step Functions, S3,
RDS, Bedrock Agents, Bedrock Knowledge Bases. Ring events are polled by the application itself and
stored in PostgreSQL running locally in Docker.

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
2. **Attach this inline policy.** It allows Bedrock invocation in us-east-1 and nothing else:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "InvokeBedrockModelsAndProfiles",
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream",
                 "bedrock:Converse", "bedrock:ConverseStream"],
      "Resource": ["arn:aws:bedrock:us-east-1::foundation-model/*",
                   "arn:aws:bedrock:us-east-1:*:inference-profile/*"]
    },
    {
      "Sid": "ReadBedrockCatalog",
      "Effect": "Allow",
      "Action": ["bedrock:ListFoundationModels", "bedrock:ListInferenceProfiles",
                 "bedrock:GetFoundationModel"],
      "Resource": "*"
    }
  ]
}
```

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

Errors are sanitized before they are stored or displayed: ARNs, access key ids, 12-digit account ids and
request ids are replaced with placeholders.
