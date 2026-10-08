from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from uuid import uuid4
from urllib.parse import urlsplit

from app.evidence.models import VerificationEvidence
from .base import Verifier
from .models import VerificationContext, VerificationStep

class BrowserVerifier(Verifier):
    _ACTIONS = frozenset({"navigate", "fill", "click", "wait_for", "assert_text", "assert_url", "assert_visible", "assert_hidden"})
    def __init__(self) -> None:
        self._sessions = {}
    def _artifact(self, context: VerificationContext) -> Path:
        root = Path(context.artifact_root).resolve() / context.run_id
        root.mkdir(parents=True, exist_ok=True)
        return root / f"failed-{uuid4().hex}.png"
    def verify(self, condition_id: str, step: VerificationStep, context: VerificationContext) -> VerificationEvidence:
        started = monotonic(); common = dict(condition_id=condition_id, verifier_type="browser", operation=step.operation, expected=step.expected, run_id=context.run_id, timestamp=datetime.now(timezone.utc).isoformat())
        if step.operation not in self._ACTIONS: return VerificationEvidence(**common, observed=None, status="BLOCKED", error="Unsupported browser action")
        try:
            from playwright.sync_api import sync_playwright
            if context.run_id not in self._sessions:
                p = sync_playwright().start(); browser = p.chromium.launch(); page = browser.new_page()
                if context.allowed_origin:
                    allowed = urlsplit(context.allowed_origin)
                    def bound_route(route):
                        requested = urlsplit(route.request.url)
                        if (requested.scheme, requested.netloc.lower()) == (allowed.scheme, allowed.netloc.lower()):
                            route.continue_()
                        else:
                            route.abort()
                    page.route("**/*", bound_route)
                self._sessions[context.run_id] = (p, browser, page)
            p, browser, page = self._sessions[context.run_id]
            target = step.target or ""; observed = None
            if step.operation == "navigate": page.goto(target, timeout=int(context.timeout_seconds*1000)); observed = page.url
            elif step.operation == "fill":
                credential_ref = step.params.get("credential_ref")
                value = context.credentials.get(credential_ref) if credential_ref else step.expected
                if value is None: raise ValueError("Credential reference is unavailable")
                page.locator(target).fill(str(value)); observed = "filled"
            elif step.operation == "click": page.locator(target).click(); observed = "clicked"
            elif step.operation == "wait_for": page.locator(target).wait_for(timeout=int(context.timeout_seconds*1000)); observed = "ready"
            elif step.operation == "assert_text": observed = page.locator(target).inner_text(); assert str(step.expected) in observed
            elif step.operation == "assert_url":
                observed = page.url
                assert observed == step.expected if step.params.get("exact") else str(step.expected) in observed
            elif step.operation == "assert_visible": observed = page.locator(target).is_visible(); assert observed is True
            else: observed = page.locator(target).is_hidden(); assert observed is True
            return VerificationEvidence(**common, observed=observed, status="VERIFIED", duration_seconds=monotonic()-started, metadata={"url": page.url, "target": target})
        except Exception as exc:
            artifacts = ()
            try:
                artifact = self._artifact(context); page.screenshot(path=str(artifact)); artifacts = (str(artifact),)
            except Exception: pass
            error = "Browser fill failed" if step.operation == "fill" else f"{type(exc).__name__}: {exc}"
            return VerificationEvidence(**common, observed=None, status="FAILED", error=error, artifacts=artifacts, duration_seconds=monotonic()-started)

    def close(self, run_id: str) -> None:
        session = self._sessions.pop(run_id, None)
        if session:
            session[1].close(); session[0].stop()
