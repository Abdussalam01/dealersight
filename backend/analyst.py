"""Grounded AI analyst on Amazon Bedrock.

Code calculates, the model explains. Bedrock never receives raw database rows:
it receives a finished metric packet (backend/packets.py) derived from them, and
it performs no arithmetic.

The model answers in a small JSON shape: a summary containing no digits, a list
of claims that each name a metric id from the packet, and things to investigate.
Every claim is then checked against the packet - metric id, value, unit and
direction - so a correct number attached to the wrong metric, wrong period or
wrong dealership is rejected, as is a decrease described as an increase. The
numbers displayed come from the packet, not from the model.

Guards, each with its own answer source:
  insufficient_data      required metrics missing; the model is never called
  fallback_rejected      the model answered but failed validation
  fallback_unavailable   Bedrock failed, timed out, or stopped abnormally
  bedrock                validated model answer
"""

import json
import random
import re
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from psycopg.types.json import Jsonb

from backend import config, packets

PROMPT_VERSION = "2026-09-23.1"
DEADLINE_SECONDS = 10          # total budget for the whole call, enforced here
READ_TIMEOUT_SECONDS = 4       # per attempt; two attempts plus backoff stay inside the deadline
MAX_WORDS = 120
ACCEPTED_STOP_REASONS = {"end_turn", "stop_sequence"}
RETRYABLE_ERRORS = {"ThrottlingException", "ServiceUnavailableException", "InternalServerException",
                    "ModelTimeoutException", "ModelNotReadyException"}
# Wording that asserts a cause. The analyst may describe what changed and what to investigate, never why.
CAUSAL_PHRASES = ("led to", "leads to", "caused", "causing", "because", "due to", "resulted in", "resulting in",
                  "drove", "driven by", "thanks to", "as a result", "responsible for", "explains the", "reflects the")
# The project rules require every answer to say that a correlation does not prove a cause.
DISCLAIMER_PHRASES = ("do not prove", "does not prove", "not prove a cause", "cannot prove", "does not establish",
                      "do not establish", "no causal", "not causal")

SYSTEM_PROMPT = f"""You explain dealership metrics for DealerSight.

Answer with a single JSON object and nothing else:
{{"summary": "...", "claims": [{{"metric_id": "...", "value": 0, "unit": "...", "direction": "..."}}],
 "investigate": ["...", "..."]}}

Rules, all mandatory:
- "summary": plain prose, at most three sentences. It must end with a sentence saying these metrics do not prove a
  cause. It must contain NO digits at all: every number belongs in
  "claims", and DealerSight prints those numbers next to your summary. Name metrics in words instead, for example
  "sales conversion fell while probable test drives held steady". Say that these metrics do not prove a cause. If
  the packet does not show the situation the question assumes, say so.
- "claims": every number you rely on, each naming a metric_id that exists in the packet's "metrics" table, with that
  metric's exact value and unit. Use "direction" ("increase", "decrease" or "flat") for percent_change and
  percentage_points metrics, matching the sign of the value. Never invent, estimate or calculate a number.
- "investigate": two or three short things a manager could look into. Never assert why something happened.
- Never write that one thing caused, drove, led to, resulted in or explains another. Describe what changed only.
- Ring-derived stages are inferred from camera activity; sales and finance records are simulated. Never describe a
  Ring event as observing a sale, a customer identity or a finance agreement.
- Keep summary and investigate under {MAX_WORDS} words in total. No marketing language.

Example of a valid answer:
{{"summary": "Simulated sales fell while probable test-drive sessions held steady, so fewer sessions converted.
These metrics describe what changed and do not prove a cause.",
  "claims": [{{"metric_id": "sales.change_pct", "value": -30.0, "unit": "percent_change", "direction": "decrease"}}],
  "investigate": ["lead follow-up time", "inventory availability"]}}"""


def bedrock_client():
    session = boto3.Session(profile_name=config.AWS_PROFILE or None, region_name=config.AWS_REGION)
    return session.client("bedrock-runtime", config=Config(
        connect_timeout=2, read_timeout=READ_TIMEOUT_SECONDS,
        # total_max_attempts=1 means exactly one SDK attempt: this module owns retries and the deadline.
        retries={"total_max_attempts": 1, "mode": "standard"}))


def sanitize(text):
    """Keep account ids, ARNs, keys and request ids out of anything we store or show."""
    if not text:
        return text
    text = re.sub(r"arn:aws[^\s\"']+", "[arn]", text)
    text = re.sub(r"\b(AKIA|ASIA)[0-9A-Z]{8,}\b", "[access-key]", text)
    text = re.sub(r"\b\d{12}\b", "[account-id]", text)
    text = re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "[request-id]", text)
    return text[:600]


def _strip_code_fence(text):
    """Some models wrap JSON in a markdown fence; unwrap it before parsing."""
    if not isinstance(text, str):
        return text
    fenced = re.match(r"\s*```(?:json)?\s*(.*?)\s*```\s*$", text, re.S)
    return fenced.group(1) if fenced else text


def validate(answer_text, packet):
    """Return (structured answer, list of reasons it fails). Empty reasons means the answer is usable."""
    reasons = []
    try:
        parsed = json.loads(_strip_code_fence(answer_text))
    except (TypeError, ValueError):
        return None, ["model answer was not valid JSON"]
    if not isinstance(parsed, dict):
        return None, ["model answer was not a JSON object"]

    summary = parsed.get("summary")
    claims = parsed.get("claims")
    investigate = parsed.get("investigate") or []
    if not isinstance(summary, str) or not summary.strip():
        reasons.append("missing summary")
        summary = ""
    if not isinstance(claims, list) or not claims:
        reasons.append("missing claims")
        claims = []
    if re.search(r"\d", summary):
        reasons.append("summary contained digits instead of naming metrics in claims")
    spoken = " ".join([summary] + [str(item) for item in investigate]).lower()
    # A disclaimer such as "does not prove the promotion caused it" is allowed; an assertion is not.
    negated = re.sub(r"\b(?:do(?:es)?\s+not|cannot|can't|never)\s+(?:prove|show|mean|imply)[^.]*", " ", spoken)
    for phrase in CAUSAL_PHRASES:
        if phrase in negated:
            reasons.append(f"answer asserted a cause: {phrase!r}")
            break
    if not any(phrase in spoken for phrase in DISCLAIMER_PHRASES):
        reasons.append("answer did not state that these metrics do not prove a cause")
    words = len(summary.split()) + sum(len(str(item).split()) for item in investigate)
    if words > MAX_WORDS:
        reasons.append(f"answer was {words} words, over the {MAX_WORDS} word limit")

    metrics = packet.get("metrics", {})
    checked = []
    for claim in claims:
        if not isinstance(claim, dict):
            reasons.append("claim was not an object")
            continue
        metric_id = claim.get("metric_id")
        metric = metrics.get(metric_id)
        if not metric:
            reasons.append(f"claim names a metric that is not in the packet: {metric_id!r}")
            continue
        value, unit, direction = claim.get("value"), claim.get("unit"), claim.get("direction")
        if not isinstance(value, (int, float)) or metric["value"] is None or abs(value - metric["value"]) > 0.05:
            reasons.append(f"{metric_id}: claimed value {value!r}, packet has {metric['value']!r}")
            continue
        if unit != metric["unit"]:
            reasons.append(f"{metric_id}: claimed unit {unit!r}, packet has {metric['unit']!r}")
            continue
        if metric["direction"] and direction != metric["direction"]:
            reasons.append(f"{metric_id}: claimed direction {direction!r}, packet has {metric['direction']!r}")
            continue
        checked.append({"metric_id": metric_id, "value": metric["value"], "unit": metric["unit"],
                        "direction": metric["direction"], "label": metric["label"], "scope": metric["scope"]})

    investigate_text = [str(item) for item in investigate][:4]
    reasons += _prose_disagreements(summary, investigate_text, checked, metrics)
    structured = {"summary": summary.strip(), "claims": checked, "investigate": investigate_text}
    return structured, reasons


RISE_WORDS = ("rose", "rise", "risen", "increase", "increased", "increasing", "grew", "growth", "up ", "higher",
              "improved", "gained")
FALL_WORDS = ("fell", "fall", "fallen", "decrease", "decreased", "decreasing", "declined", "decline", "dropped",
              "drop", "down ", "lower", "reduced", "worsened")
FLAT_WORDS = ("held steady", "stayed flat", "stayed the same", "remained steady", "unchanged", "remained the same",
              "held flat", "flat")


def _subject_words(metric_id):
    """Words that identify the metric a sentence is about, e.g. 'sales.change_pct' -> ('sales',)."""
    stem = metric_id.split(".")[-2] if metric_id.count(".") > 1 else metric_id.split(".")[0]
    return tuple(word for word in stem.replace("_", " ").split() if len(word) > 3)


def _prose_disagreements(summary, investigate, claims, metrics):
    """Reject prose that contradicts a validated claim, or numbers that no claim supports."""
    reasons = []
    for sentence in re.split(r"[.;]", summary.lower()):
        for claim in claims:
            if claim["direction"] is None:
                continue
            subject = _subject_words(claim["metric_id"])
            if not subject or not all(word in sentence for word in subject):
                continue
            said_up = any(word in sentence for word in RISE_WORDS)
            said_down = any(word in sentence for word in FALL_WORDS)
            said_flat = any(word in sentence for word in FLAT_WORDS)
            direction = claim["direction"]
            if (direction == "increase" and said_down and not said_up) or                (direction == "decrease" and said_up and not said_down) or                (direction == "flat" and (said_up or said_down) and not said_flat):
                reasons.append(f"summary describes {claim['metric_id']} as the opposite of its {direction}")
    supported = {round(float(metric["value"]), 2) for metric in metrics.values() if metric["value"] is not None}
    supported |= {round(-value, 2) for value in supported}
    for token in re.findall(r"-?\d+(?:\.\d+)?", " ".join(investigate)):
        value = round(float(token), 2)
        if not any(abs(value - candidate) <= 0.05 for candidate in supported):
            reasons.append(f"investigation text used a number that is not in the packet: {value}")
    return reasons


def fallback_answer(packet):
    """Deterministic structured answer built from the packet, used whenever the model's answer is not usable."""
    metrics = packet.get("metrics", {})
    question = packet["question_id"]
    pick = {
        "q1_conversion_decline": ["visits.change_pct", "probable_test_drives.change_pct", "sales.change_pct",
                                  "sales_conversion.period_a", "sales_conversion.period_b"],
        "q3_promotion_performance": ["probable_test_drives.change_pct", "sales.change_pct",
                                     "finance_penetration.before", "finance_penetration.during",
                                     "finance_penetration.change_points"],
    }.get(question)
    if question == "q2_test_drives_without_sales":
        pick = [f"{row['dealer'].lower().replace(' ', '_')}.{key}.change_pct"
                for row in packet["dealers"][:3] for key in ("probable_test_drives", "sales")]

    summaries = {
        "q1_conversion_decline": "These metrics describe what changed at this dealership between the two periods. "
                                 "They do not prove a cause.",
        "q2_test_drives_without_sales": "Dealerships are ranked by probable test-drive growth minus sales growth, "
                                        "computed in application code. The ranking does not prove a cause.",
        "q3_promotion_performance": "The promotion period is compared with an equal period before it. A difference "
                                    "between the periods does not prove the promotion caused it.",
    }
    investigate = {
        "q1_conversion_decline": ["staffing during opening hours", "inventory availability", "lead follow-up time"],
        "q2_test_drives_without_sales": ["follow-up after a test drive", "pricing against nearby dealerships"],
        "q3_promotion_performance": ["promotion awareness in store", "finance approval times"],
    }[question]
    claims = [{"metric_id": key, "value": metrics[key]["value"], "unit": metrics[key]["unit"],
               "direction": metrics[key]["direction"], "label": metrics[key]["label"], "scope": metrics[key]["scope"]}
              for key in pick if key in metrics and metrics[key]["value"] is not None]
    return {"summary": summaries[question], "claims": claims, "investigate": investigate}


def as_text(structured):
    """Plain-text rendering for the audit log; the UI renders the same structure with labels."""
    if not structured:
        return ""
    suffix = {"percent": "%", "percent_change": "%", "percentage_points": " points", "count": ""}
    lines = [f"{claim['label']}: {claim['value']}{suffix.get(claim['unit'], '')}"
             + (f" ({claim['direction']})" if claim["direction"] else "") for claim in structured["claims"]]
    investigate = "; ".join(structured["investigate"])
    return " ".join(filter(None, [structured["summary"], " | ".join(lines),
                                  f"Worth investigating: {investigate}." if investigate else ""]))


class DeadlineExceeded(Exception):
    """The answer did not arrive inside the analyst's total budget."""


def _call_bedrock(client, packet, model_id, deadline=None):
    """One call inside a total deadline, with a single retry for transient errors only."""
    deadline = deadline if deadline is not None else time.monotonic() + DEADLINE_SECONDS
    attempt = 0
    while True:
        attempt += 1
        if time.monotonic() >= deadline:
            raise DeadlineExceeded(f"no answer within {DEADLINE_SECONDS}s")
        try:
            response = client.converse(
                modelId=model_id,
                system=[{"text": SYSTEM_PROMPT}],
                messages=[{"role": "user", "content": [{"text": json.dumps(packet, default=str)}]}],
                inferenceConfig={"maxTokens": 600, "temperature": 0},
            )
            if time.monotonic() > deadline:   # arrived too late to use
                raise DeadlineExceeded(f"answer arrived after the {DEADLINE_SECONDS}s deadline")
            return response
        except DeadlineExceeded:
            raise
        except Exception as exc:
            code = exc.response["Error"]["Code"] if isinstance(exc, ClientError) else type(exc).__name__
            transient = code in RETRYABLE_ERRORS or "Timeout" in code or "timeout" in str(exc).lower()
            if attempt > 1 or not transient or time.monotonic() >= deadline - 1:
                raise
            time.sleep(min(0.3 + random.random() * 0.5, max(0.0, deadline - time.monotonic())))


def ask(conn, question_id, dealer_id=None, client=None, now=None, model_id=None):
    """Build the packet, call Bedrock when the data supports it, validate, and log every call."""
    packet = packets.build(conn, question_id, dealer_id, now)
    model_id = model_id or config.BEDROCK_MODEL_ID
    result = {"question_id": question_id, "question": packet["question"], "packet": packet, "model_id": model_id,
              "latency_ms": None, "error": None, "model_answer": None, "stop_reason": None, "usage": None,
              "prompt_version": PROMPT_VERSION, "packet_schema_version": packet.get("packet_schema_version")}

    if packet.get("insufficient_data"):
        result |= {"answer": {"summary": f"Insufficient data: {packet['insufficient_data']}.", "claims": [],
                              "investigate": []}, "answer_source": "insufficient_data"}
        return _log(conn, result, dealer_id)

    started, started_monotonic = time.perf_counter(), time.monotonic()
    try:
        client = client or bedrock_client()   # inside the guard: a bad profile must fall back, not crash
        response = _call_bedrock(client, packet, model_id, deadline=started_monotonic + DEADLINE_SECONDS)
        result["latency_ms"] = round((time.perf_counter() - started) * 1000)
        result["stop_reason"] = response.get("stopReason")
        result["usage"] = response.get("usage")
        answer_text = response["output"]["message"]["content"][0]["text"].strip()
        result["model_answer"] = answer_text[:4000]
        if result["stop_reason"] and result["stop_reason"] not in ACCEPTED_STOP_REASONS:
            result |= {"answer": fallback_answer(packet), "answer_source": "fallback_unavailable",
                       "error": f"model stopped with stopReason={result['stop_reason']}"}
            return _log(conn, result, dealer_id)
        structured, reasons = validate(answer_text, packet)
        if reasons:
            result |= {"answer": fallback_answer(packet), "answer_source": "fallback_rejected",
                       "error": "answer rejected: " + "; ".join(reasons)[:400]}
        else:
            result |= {"answer": structured, "answer_source": "bedrock"}
    except Exception as exc:
        result["latency_ms"] = round((time.perf_counter() - started) * 1000)
        result |= {"answer": fallback_answer(packet), "answer_source": "fallback_unavailable",
                   "error": sanitize(f"{type(exc).__name__}: {exc}")}
    return _log(conn, result, dealer_id)


def _log(conn, result, dealer_id):
    usage = result.get("usage") or {}
    conn.execute(
        """INSERT INTO analyst_log (question_id, dealer_id, packet, answer, model_answer, answer_source, model_id,
                                    latency_ms, error, prompt_version, packet_schema_version, stop_reason,
                                    input_tokens, output_tokens)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (result["question_id"], dealer_id, Jsonb(result["packet"]), as_text(result["answer"]), result["model_answer"],
         result["answer_source"], result["model_id"], result["latency_ms"], sanitize(result["error"]),
         result["prompt_version"], result["packet_schema_version"], result["stop_reason"],
         usage.get("inputTokens"), usage.get("outputTokens")),
    )
    return result


def recent_calls(conn, limit=10):
    limit = max(1, min(int(limit), 50))
    return conn.execute(
        """SELECT asked_at, question_id, answer_source, model_id, latency_ms, stop_reason, input_tokens,
                  output_tokens, error, left(answer, 200) AS answer
           FROM analyst_log ORDER BY asked_at DESC LIMIT %s""", (limit,)).fetchall()
