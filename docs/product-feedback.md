# Product Feedback

Written during development (Sept 21–27, 2026), not reconstructed at the end. Related friction-log entries
are referenced as FL-xx; sanitized evidence is in `docs/evidence/`.

---

## 1. Ring Partner API (`https://api.amazonvision.com`)

**1. What was it used for?**
The whole Ring integration. DealerSight discovers the camera with `GET /v1/devices` and reads camera
activity with `GET /v1/history/devices/{device_id}/events`, polled every five seconds from
`backend/ring_client.py`. Those events become the first three stages of the dealership funnel.

**2. What worked well?**
Plain REST with OAuth bearer tokens meant no SDK to learn: the first successful device call took minutes
using `requests`. JSON:API responses are consistent and predictable, event ids are stable, which made
idempotency straightforward, and `start`/`end` epoch milliseconds gave us an event duration we used for
the vehicle-area engagement rule. The docs are unusually candid about hard parts: server-to-server only
because of CORS, the exact HMAC encoding difference between webhooks (hex) and nonces (URL-safe base64),
and a "common mistakes" section that named the re-serialized-JSON signature trap before we hit it.
Documenting Event History polling as an official alternative to webhooks let us build a complete
integration without a registered app.

**3. What needs improvement?**
- The `event_types` filter is silently ignored for Playground devices. Every value we tried, including
  the documented composite syntax `motion.human,motion.vehicle`, a control value (`ding`, with no
  doorbell presses ever made) and an invalid value, returned the same events with HTTP 200 (FL-02,
  `docs/evidence/FL-02_event_types_filter_test.json`). Rejecting unsupported values with 400 would have
  saved an hour of doubting our own code.
- History and webhook payloads describe the same event differently: `attributes.event_type` plus
  `start`/`end` plus `relationships.source.data.id` versus `data.type` plus `attributes.timestamp` plus
  `attributes.source`. The subtype lists also differ; history documents `motion.animal`, the webhook
  `sub_type` table does not (FL-05). A mapping table between the two surfaces would help.
- `timestamp_readable` has no timezone offset, which invites mistakes; we use the epoch value everywhere.

**4. Onboarding, zero to a working example?**
About twenty minutes from a free Ring account to real data in our own code, using a Playground token and
the hello-world scripts. No app registration, no account linking, no subscription.

**5. Would we build with it again?**
Yes. The API is well shaped for event-driven products, and the honesty of the documentation about
constraints is worth more than a polished SDK.

---

## 2. Ring Developer Playground

**1. What was it used for?**
The accepted Ring simulator for the whole project: one-click tokens and simulated events, which are the
live Ring moments in the demonstration.

**2. What worked well?**
A working token in one click with no app registration is the single best thing about the Ring developer
experience. Events appeared in history within about a minute and could be triggered repeatedly, which
made a rehearsable demo possible.

**3. What needs improvement?**
- **Motion, Vehicle and Package triggers are indistinguishable.** All three are recorded in history as
  `event_type: "on_demand"` with an empty `cv_detections` array, so nothing says which button was pressed
  (FL-01, `docs/evidence/FL-01_playground_triggers_on_demand.json`). Our product is built on what the
  camera saw, so this forced a redesign: DealerSight now treats every Ring event as generic camera
  activity and takes the zone from the camera's configured placement instead. Recording the simulated
  trigger as a `motion` event with the matching `sub_type` would let simulator users build and test
  detection-driven features.
- **Only one synthetic device.** No documented way to add another (FL-03). Multi-camera business use
  cases cannot be tested directly; we simulate multiple placements by temporarily assigning the single
  camera to a position, clearly labelled in the UI. Several synthetic devices, or one synthetic
  multi-camera device exposing `component_ids`, would fix this.
- **No webhook path.** The temporary token gives no documented way to configure or test webhook delivery
  (FL-04), so the signature-verification code we wrote to spec is still unexercised. A Playground webhook
  tester that posts signed sample payloads to a developer-supplied URL would be valuable.
- `on_demand` means a live-view session, so an application that requests media creates events that look
  like activity. Worth calling out in the Playground docs.

**4. Onboarding, zero to a working example?**
Excellent, minutes. The Playground is only described in the release notes, though; the development guide
does not mention it, so it is easy to miss.

**5. Would we build with it again?**
Yes for connectivity, auth and payload shape. Not sufficient on its own for anything that depends on what
the camera detected.

---

## 3. `AmazonAppDev/ring-api-helloworld`

**1. What was it used for?**
First contact with the API: `list_devices.py` and `event_history.py` against a Playground token, before
writing our own client.

**2. What worked well?**
It ran unmodified on the first try. Printing the equivalent `curl` command for each call is a small,
excellent touch that made the request shape obvious. Clear separation between access-token mode and
refresh-token mode.

**3. What needs improvement?**
The Next.js dashboard is a large part of the repository but is unusable with a Playground token, since
webhooks and the event dashboard require refresh-token mode. A short table at the top showing which
features work in which mode would set expectations. A minimal polling example would also help, given that
polling is the documented alternative to webhooks.

**4. Onboarding, zero to a working example?**
Under ten minutes for the Python scripts.

**5. Would we build with it again?**
Yes as a reference. We wrote our own client because our stack is FastAPI and our needs are narrow.

---

## 4. Amazon Bedrock (Converse API)

**1. What was it used for?**
The grounded analyst: three fixed questions, each answered from a metric packet computed by DealerSight.
Model `amazon.nova-pro-v1:0` in us-east-1, called from `backend/analyst.py`. See `docs/aws.md`.

**2. What worked well?**
The Converse API is the same shape across model families, so comparing models meant changing one string.
First successful call took about ten minutes from an empty AWS account. Responses carry `usage` token
counts and `stopReason`, which we log and display; `stopReason` in particular lets us reject an answer
that ended because of a token limit or a content filter rather than because it finished. Latency was
about 1 second for a 3,300-token packet, fast enough to use live on stage. `temperature: 0` gave stable
output across repeated runs.

**3. What needs improvement?**
- **Model access failures are hard to tell apart.** An IAM gap and a missing use-case submission both
  surface as access errors with quite different remedies (`AccessDeniedException` naming
  `bedrock:InvokeModel`, versus `ResourceNotFoundException` saying "Model use case details have not been
  submitted"). The second message does not say where to submit them.
- **The use-case form is invisible at the point of choosing a model.** Anthropic models appear in
  `list_foundation_models` and in the console catalogue with no indication that an extra step is needed.
- **Inference-profile ids are guesswork without an extra permission.** `ListInferenceProfiles` is a
  separate IAM action, so a developer who has `InvokeModel` still cannot discover that a model needs the
  `us.` prefix. The error for calling the bare model id does not suggest the profile id.
- **Structured output is by convention only.** Asked for a single JSON object, `us.amazon.nova-2-lite-v1:0`
  wrapped its JSON in a markdown fence on every call, failing our parser until we unwrapped fences. A
  documented response-format option, or a note that fencing is common, would save that debugging.
- **Instruction adherence varies in ways that matter for compliance.** Our prompt requires every answer to
  state that a correlation does not prove a cause. Nova Pro omitted that sentence in 2 of 12 answers
  before we enforced it in code, and Nova Micro twice put a number in a field we required to be free of
  digits. None of this is a defect in Bedrock, but it is a strong argument that the guarantees have to
  live in the application, not the prompt.
- Given no facts, models invent them: asked only to greet DealerSight, Nova Micro described it as
  providing "vehicle inspection and valuation services" (FL-07,
  `docs/evidence/FL-07_bedrock_tone_test.json`). That result shaped the entire design of our analyst.

**4. Onboarding, zero to a working example?**
Quick for Amazon models: create a user, attach a Bedrock policy, enable model access, ten lines of boto3.
Slower for third-party models because of the extra approval step described above.

**5. Would we build with it again?**
Yes. Consistent API, useful response metadata, and a wide model catalogue behind one interface. Our advice
to anyone doing analytics with it: compute every number in application code and validate the model's
output against those numbers before showing it to anyone.

---

## 5. boto3 / botocore

**1. What was it used for?**
The Bedrock client, including timeouts and retry behaviour (`botocore.config.Config`).

**2. What worked well?**
Per-client `connect_timeout`, `read_timeout` and retry mode made it straightforward to hold the analyst
to a predictable deadline. Errors carry structured codes, so we can retry throttling and 5xx while
failing fast on access denial.

**3. What needs improvement?**
The interaction between `retries.max_attempts` and per-attempt `read_timeout` is easy to misjudge: the
documented knobs are per attempt, so the worst-case wall-clock time is a multiple that is never stated.
For an interactive feature we ended up implementing our own deadline around the call. A
`total_timeout`-style option would be a welcome addition.

**4. Onboarding, zero to a working example?**
Immediate; `pip install boto3` and the shared credentials file worked with no surprises.

**5. Would we build with it again?**
Yes.

---

## 6. AWS services used, for the AWS Builder mini challenge

| Service | Region | Purpose | Code |
|---|---|---|---|
| Amazon Bedrock (Converse, `amazon.nova-pro-v1:0`) | us-east-1 | Explains computed metric packets for the three analyst questions | `backend/analyst.py` |
| AWS IAM | global | Project-scoped user permitted only to invoke Bedrock models in us-east-1 | policy in `docs/aws.md` |

No other AWS service is used, and none appears in the architecture diagram. Full setup instructions,
failure handling and the model benchmark are in `docs/aws.md`.
