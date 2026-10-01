from __future__ import annotations

from datetime import datetime, timezone
from time import monotonic
from urllib.parse import urljoin

import httpx

from app.evidence.models import VerificationEvidence
from .base import Verifier
from .models import VerificationContext, VerificationStep

class HTTPVerifier(Verifier):
    _OPERATIONS = frozenset({"reachable", "status", "json_value", "contains", "redirect"})
    def verify(self, condition_id: str, step: VerificationStep, context: VerificationContext) -> VerificationEvidence:
        started, url = monotonic(), urljoin((context.base_url or "").rstrip("/") + "/", step.target or "")
        common = dict(condition_id=condition_id, verifier_type="http", operation=step.operation, expected=step.expected, run_id=context.run_id, timestamp=datetime.now(timezone.utc).isoformat())
        if step.operation not in self._OPERATIONS or not url:
            return VerificationEvidence(**common, observed=None, status="BLOCKED", error="Unsupported HTTP operation or missing target")
        try:
            response = httpx.request(step.params.get("method", "GET"), url, timeout=context.timeout_seconds, follow_redirects=False)
            body = response.text[:1000]
            observed, status = response.status_code, "INCONCLUSIVE"
            if step.operation == "reachable": status, observed = "VERIFIED", True
            elif step.operation == "status": status = "VERIFIED" if response.status_code == step.expected else "FAILED"
            elif step.operation == "contains": observed, status = (str(step.expected) in body), ("VERIFIED" if str(step.expected) in body else "FAILED")
            elif step.operation == "redirect":
                observed = response.headers.get("location")
                status = "VERIFIED" if observed == step.expected else "FAILED"
            elif step.operation == "json_value":
                try:
                    value = response.json()
                    for part in str(step.params.get("path", "")).split("."):
                        value = value[part]
                    observed, status = value, ("VERIFIED" if value == step.expected else "FAILED")
                except (ValueError, KeyError, TypeError): observed, status = "malformed-or-missing-json-path", "INCONCLUSIVE"
            return VerificationEvidence(**common, observed=observed, status=status, duration_seconds=monotonic()-started, metadata={"url": url, "method": response.request.method, "status_code": response.status_code, "body_excerpt": body, "redirect_location": response.headers.get("location")})
        except httpx.ConnectError as exc:
            return VerificationEvidence(**common, observed=None, status="BLOCKED", error=str(exc), duration_seconds=monotonic()-started, metadata={"url": url})
        except httpx.TimeoutException as exc:
            return VerificationEvidence(**common, observed=None, status="INCONCLUSIVE", error=str(exc), duration_seconds=monotonic()-started, metadata={"url": url})
        except httpx.HTTPError as exc:
            return VerificationEvidence(**common, observed=None, status="BLOCKED", error=str(exc), duration_seconds=monotonic()-started, metadata={"url": url})

from app.core.contracts import OutcomeContract
from app.evidence.models import VerificationReport


async def verify_http_contract(base_url: str, contract: OutcomeContract) -> VerificationReport:
    # Implementation remains in the compatibility module during this mechanical move.
    from app.engine import verify_contract
    return await verify_contract(base_url, contract)
