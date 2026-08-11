"""Automated log triage pipeline: dedup -> normalize -> Gemini triage -> mock Slack."""

import hashlib
import os
import re
import time
from collections import Counter

from google import genai
from google.genai import errors, types
from pydantic import BaseModel, Field, model_validator

# Free tier is 20 requests/day PER MODEL, so the model you pick is a budget choice.
# Override for a stronger verdict: TRIAGE_MODEL=gemini-3.6-flash python mockapp.py
MODEL = os.getenv("TRIAGE_MODEL", "gemini-3.1-flash-lite")
RPM_SLEEP = 4  # base pacing for the free tier; the client's retry_options absorb the rest


class TriageVerdict(BaseModel):
    root_cause: str = Field(description="The underlying cause of the error.")
    severity: str = Field(description="Severity level: CRITICAL, HIGH, MEDIUM, LOW.")
    proposed_fix: str = Field(
        description="Actionable steps or code changes to fix the issue."
    )
    confidence: float = Field(
        ge=0.0, le=1.0, description="Confidence score from 0.0 to 1.0."
    )
    needs_human_review: bool = Field(description="True if confidence is below 0.8.")

    @model_validator(mode="after")
    def _enforce_review_flag(self):
        # Never trust the model to compute this: it is a routing decision.
        self.needs_human_review = self.confidence < 0.8
        return self


def normalize(text: str) -> str:
    """Scrub volatile data so the same error hashes identically every time."""
    text = re.sub(r"\d{4}-\d{2}-\d{2}T[\d:.]+Z?", "<TS>", text)
    text = re.sub(r"\b[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", "<UUID>", text)
    text = re.sub(r"\d+", "<N>", text)  # no \b: must also catch "5031ms", "line42"
    return text.strip()


def fingerprint(log: dict) -> str:
    """Stable MD5 over serviceName + error.class + message."""
    key = f"{log['serviceName']}|{log['error']['class']}|{normalize(log['message'])}"
    return hashlib.md5(key.encode()).hexdigest()


class TriagePipeline:
    def __init__(self, threshold: int = 3):
        self.threshold = threshold
        self.counts: Counter[str] = Counter()
        self.samples: dict[str, dict] = {}
        # The SDK retries 429/503 with exponential backoff itself — no hand-rolled retry loop.
        self.client = (
            genai.Client(
                http_options=types.HttpOptions(
                    retry_options=types.HttpRetryOptions(
                        attempts=5, initial_delay=10, max_delay=90, http_status_codes=[429, 503]
                    )
                )
            )
            if os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
            else None
        )

    def ingest(self, logs: list[dict]):
        """Stage 1 & 2: dedup gate + normalization."""
        for log in logs:
            if log.get("level") not in ("ERROR", "FATAL"):
                continue
            fp = fingerprint(log)
            self.counts[fp] += 1
            self.samples.setdefault(fp, log)

    def run(self, interactive: bool = False):
        """Stage 3 & 4: LLM triage loop, then routing."""
        hot = [fp for fp, n in self.counts.items() if n >= self.threshold]
        print(
            f"\n{len(self.counts)} unique signatures, {len(hot)} over threshold ({self.threshold}).\n"
        )
        if interactive:
            return self._run_interactive(hot)
        for fp in hot:
            self._triage_one(fp)
            time.sleep(RPM_SLEEP)

    def _triage_one(self, fp: str):
        log, n = self.samples[fp], self.counts[fp]
        print(f"🔎 Triaging {fp[:12]} — {log['serviceName']} / {log['error']['class']} ({n}x)")
        self.alert(log, n, self.triage(log, n))

    def _run_interactive(self, hot: list[str]):
        """One API call per pick. The free tier is 20/day — nothing is spent unasked."""
        done: set[str] = set()
        while True:
            print(f"signatures over threshold ({self.threshold}) — model: {MODEL}")
            for i, fp in enumerate(hot, 1):
                log = self.samples[fp]
                mark = "  [done]" if fp in done else ""
                print(
                    f"  {i}  {log['serviceName']:<20} {log['error']['class']:<22}"
                    f" {self.counts[fp]}x{mark}"
                )
            try:
                choice = input("pick a number to triage, or q to quit: ").strip()
            except EOFError:  # piped/redirected run — spend nothing
                print("(no tty — nothing triaged)")
                return
            if choice.lower() in ("q", "quit", ""):
                return
            if not choice.isdigit() or not 1 <= int(choice) <= len(hot):
                print(f"  ? pick 1-{len(hot)} or q\n")
                continue
            fp = hot[int(choice) - 1]
            print()
            self._triage_one(fp)
            done.add(fp)
            time.sleep(RPM_SLEEP)

    def _fetch_context(self, log: dict) -> str:
        """Related log lines. Base pipeline has no log store — subclass to supply one."""
        return ""

    def triage(self, log: dict, count: int) -> TriageVerdict:
        if self.client is None:
            print("   ⚠️  No GEMINI_API_KEY — using stub verdict.")
            return TriageVerdict(
                root_cause=f"[stub] {log['error']['class']} in {log['serviceName']}",
                severity="MEDIUM",
                proposed_fix="Set GEMINI_API_KEY for a real verdict.",
                confidence=0.5,
                needs_human_review=True,
            )
        prompt = (
            f"Triage this production error. It fired {count} times.\n\n"
            f"service: {log['serviceName']}\n"
            f"class: {log['error']['class']}\n"
            f"message: {log['message']}\n"
            f"stack: {log['error'].get('stack', 'n/a')}\n"
        )
        context = self._fetch_context(log)
        if context:
            prompt += f"\nrelated log lines leading up to the error:\n{context}\n"
        try:
            resp = self.client.models.generate_content(
                model=MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=TriageVerdict,
                ),
            )
            return resp.parsed
        except errors.APIError as e:  # ClientError (4xx) + ServerError (5xx)
            # ponytail: no retry — a failed triage escalates instead of vanishing.
            # Add tenacity-style backoff if 429s become routine.
            print(f"   ⚠️  API {e.code}: {e.message}")
            return TriageVerdict(
                root_cause=f"Triage failed: API {e.code}. Error unanalyzed.",
                severity="UNKNOWN",
                proposed_fix="Re-run triage for this signature.",
                confidence=0.0,
                needs_human_review=True,
            )

    def alert(self, log: dict, count: int, v: TriageVerdict):
        """Stage 5: mock Slack routing."""
        head = f"{log['serviceName']} · {log['error']['class']} · {count}x"
        if v.needs_human_review:
            print(
                f"   🚨 #oncall-escalation | [HUMAN REVIEW] {v.severity} — {head}\n"
                f"      cause: {v.root_cause}\n"
                f"      confidence: {v.confidence:.2f} (below 0.8 — do not auto-apply)\n"
                f"      suggested: {v.proposed_fix}\n"
                f"      → assign an engineer\n"
            )
        else:
            print(
                f"   ✅ #auto-triage | {v.severity} — {head}\n"
                f"      cause: {v.root_cause}\n"
                f"      fix: {v.proposed_fix}  (confidence {v.confidence:.2f})\n"
            )


def generate_mock_logs() -> list[dict]:
    """Mock stream. Two signatures cross a threshold of 3; the rest do not."""

    def err(svc, cls, msg, stack, level="ERROR"):
        return {
            "serviceName": svc,
            "level": level,
            "message": msg,
            "error": {"class": cls, "stack": stack},
        }

    logs = []
    # 4x — same signature, volatile ids differ (normalization must collapse these)
    for uid in (
        "123a4567-e89b-12d3-a456-426614174000",
        "987b6543-e21b-34d3-b890-426614174111",
        "555c4444-e11b-22d2-c777-426614174222",
        "77df1290-aa1b-49f1-9d0c-426614174333",
    ):
        logs.append(
            err(
                "auth-service",
                "NullPointerException",
                f"token was None for user_id={uid} at 2026-08-10T10:00:00Z",
                "at auth.middleware.verify(middleware.py:42)",
            )
        )
    # 3x — hits threshold exactly
    for ms in (5031, 5044, 5102):
        logs.append(
            err(
                "checkout-api",
                "TimeoutError",
                f"upstream payments-gateway did not respond within {ms}ms",
                "at checkout.charge(charge.py:118)",
            )
        )
    # 2x — below threshold, must be skipped
    for i in (7, 9):
        logs.append(
            err(
                "inventory-worker",
                "IntegrityError",
                f"duplicate key value violates unique constraint sku_idx (attempt {i})",
                "at inventory.sync(sync.py:64)",
            )
        )
    # noise that must never reach the LLM
    logs.append(
        {
            "serviceName": "auth-service",
            "level": "INFO",
            "message": "user logged in successfully",
            "error": {"class": "", "stack": ""},
        }
    )
    logs.append(
        err(
            "cdn-edge",
            "ConnectionReset",
            "peer closed connection mid-stream",
            "n/a",
            level="WARN",
        )
    )
    return logs


def _self_check():
    logs = generate_mock_logs()
    p = TriagePipeline(threshold=3)
    p.ingest(logs)
    assert sum(p.counts.values()) == 9, p.counts  # 4 + 3 + 2 errors, noise dropped
    assert len(p.counts) == 3, p.counts  # collapsed into 3 signatures
    hot = sorted(n for n in p.counts.values() if n >= 3)
    assert hot == [3, 4], hot  # inventory (2x) stays below
    assert TriageVerdict(
        root_cause="a",
        severity="LOW",
        proposed_fix="b",
        confidence=0.79,
        needs_human_review=False,
    ).needs_human_review
    assert not TriageVerdict(
        root_cause="a",
        severity="LOW",
        proposed_fix="b",
        confidence=0.81,
        needs_human_review=True,
    ).needs_human_review
    print("self-check ok")


if __name__ == "__main__":
    _self_check()
    p = TriagePipeline(threshold=3)
    p.ingest(generate_mock_logs())
    p.run()
