"""Small, injectable OpenAI-compatible Nebius Token Factory client."""
from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from enum import Enum
from time import monotonic
from typing import Protocol
from urllib.parse import urlsplit

import httpx

DEFAULT_MODEL = "nvidia/Nemotron-3_5-Lightning"
DEFAULT_BASE_URL = "https://api.tokenfactory.nebius.com/v1"
MAX_RESPONSE_BYTES = 131_072
MAX_CONTENT_CHARS = 32_768
MAX_OUTPUT_TOKENS = 4096
MAX_REPORTED_TOKEN_COUNT = 1_000_000
DEFAULT_READ_TIMEOUT_SECONDS = 60.0
MIN_READ_TIMEOUT_SECONDS = 10.0
MAX_READ_TIMEOUT_SECONDS = 120.0
CONNECT_TIMEOUT_SECONDS = 10.0
WRITE_TIMEOUT_SECONDS = 10.0
POOL_TIMEOUT_SECONDS = 10.0


def _read_timeout_seconds(override: float | None) -> float:
    configured = override if override is not None else os.getenv("PROOF_NEBIUS_READ_TIMEOUT_SECONDS")
    if configured is None:
        return DEFAULT_READ_TIMEOUT_SECONDS
    try:
        if type(configured) not in (int, float, str):
            raise ValueError
        seconds = float(configured)
    except (ValueError, OverflowError):
        raise ModelError("invalid_configuration", "Token Factory read timeout is invalid") from None
    if not math.isfinite(seconds) or not MIN_READ_TIMEOUT_SECONDS <= seconds <= MAX_READ_TIMEOUT_SECONDS:
        raise ModelError("invalid_configuration", "Token Factory read timeout is outside safe bounds")
    return seconds


class ReasoningPolicy(Enum):
    """Trusted request policy; stages select their own policy explicitly."""

    PROVIDER_DEFAULT = "provider_default"
    DISABLED = "disabled"


def validate_output_budget(value: int) -> int:
    if type(value) is not int or not 1 <= value <= MAX_OUTPUT_TOKENS:
        raise ValueError(f"max_output_tokens must be an integer between 1 and {MAX_OUTPUT_TOKENS}")
    return value


class ContentParseError(ValueError):
    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContentParseError("duplicate_keys")
        result[key] = value
    return result


def _reject_constant(value):
    raise ContentParseError("non_standard_constant")


def _strict_decoder() -> json.JSONDecoder:
    return json.JSONDecoder(object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)


def parse_one_json_object(content: str) -> dict:
    """Accept one object, optionally preceded by prose or enclosed in one JSON fence."""
    if not isinstance(content, str) or not content.strip():
        raise ContentParseError("empty_content")
    if len(content) > MAX_CONTENT_CHARS:
        raise ContentParseError("content_too_large")
    source = content.strip()
    opening = source.find("```")
    first_brace = source.find("{")
    if opening >= 0 and (first_brace < 0 or opening < first_brace):
        prefix = source[:opening]
        if "{" in prefix or "}" in prefix:
            raise ContentParseError("multiple_objects")
        fenced = source[opening:]
        match = re.fullmatch(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n```[ \t]*", fenced,
                             flags=re.IGNORECASE | re.DOTALL)
        if not match or "```" in match.group(1):
            raise ContentParseError("invalid_fence_or_trailing_content")
        source = match.group(1).strip()
    start = source.find("{")
    if start < 0:
        raise ContentParseError("no_json_object")
    if "}" in source[:start]:
        raise ContentParseError("invalid_prefix")
    try:
        result, end = _strict_decoder().raw_decode(source[start:])
    except json.JSONDecodeError:
        raise ContentParseError("json_syntax") from None
    if not isinstance(result, dict):
        raise ContentParseError("non_object_root")
    if source[start + end:].strip():
        raise ContentParseError("trailing_content")
    return result


def _safe_token_count(value) -> int | None:
    return value if type(value) is int and 0 <= value <= MAX_REPORTED_TOKEN_COUNT else None


def _usage_counts(body) -> tuple[int | None, int | None, int | None]:
    usage = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(usage, dict):
        return None, None, None
    details = usage.get("completion_tokens_details")
    reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else None
    return (_safe_token_count(usage.get("prompt_tokens")),
            _safe_token_count(usage.get("completion_tokens")),
            _safe_token_count(reasoning))


def _safe_response_error(category: str, status: int, content_type: str, body=None, choice=None, message=None) -> ModelError:
    known_fields = {"id", "object", "created", "model", "choices", "usage", "service_tier", "system_fingerprint"}
    fields = sorted(known_fields.intersection(body)) if isinstance(body, dict) else []
    if isinstance(body, dict) and set(body) - known_fields:
        fields.append("other")
    reason = choice.get("finish_reason") if isinstance(choice, dict) else None
    if reason not in (None, "stop", "length", "content_filter", "tool_calls", "function_call"):
        reason = "other"
    content = message.get("content") if isinstance(message, dict) else None
    chars = len(content) if isinstance(content, str) else 0
    reasoning_values = ([message[key] for key in ("reasoning", "reasoning_content") if key in message]
                        if isinstance(message, dict) else [])
    reasoning_strings = [value for value in reasoning_values if isinstance(value, str)]
    reasoning_chars = sum(len(value) for value in reasoning_strings) if reasoning_strings else None
    _, completion_tokens, reasoning_tokens = _usage_counts(body)
    metric = lambda value: str(value) if value is not None else "null"
    mime = content_type.split(";", 1)[0].strip().lower()
    if mime not in ("application/json", "application/problem+json"):
        mime = "other"
    diagnostics = (f"category={category}; status={status}; content_type={mime}; fields={','.join(fields)}; "
                   f"finish_reason={reason or 'missing'}; content_exists={content is not None}; "
                   f"content_chars={chars}; reasoning_field_present={bool(reasoning_values)}; "
                   f"reasoning_content_nonempty={any(value.strip() for value in reasoning_strings)}; "
                   f"reasoning_chars={metric(reasoning_chars)}; "
                   f"completion_tokens={metric(completion_tokens)}; "
                   f"reasoning_tokens={metric(reasoning_tokens)}")
    return ModelError("malformed_response", f"Token Factory response invalid ({diagnostics})")


class ModelError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ModelUsage:
    model_id: str
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_seconds: float
    request_count: int = 1
    reasoning_tokens: int | None = None


@dataclass(frozen=True)
class ModelResponse:
    data: dict
    usage: ModelUsage


class ModelClient(Protocol):
    model_id: str

    def complete_json(self, system: str, user: str, *, max_output_tokens: int = 800,
                      reasoning_policy: ReasoningPolicy = ReasoningPolicy.PROVIDER_DEFAULT) -> ModelResponse: ...


class TokenFactoryClient:
    def __init__(self, *, transport: httpx.BaseTransport | None = None, api_key: str | None = None,
                 model_id: str | None = None, base_url: str | None = None, retries: int = 1,
                 read_timeout_seconds: float | None = None) -> None:
        self._api_key = api_key if api_key is not None else os.getenv("NEBIUS_API_KEY", "")
        self.model_id = model_id or os.getenv("PROOF_NEMOTRON_MODEL", DEFAULT_MODEL)
        self._base_url = (base_url or os.getenv("NEBIUS_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        parsed = urlsplit(self._base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ModelError("invalid_configuration", "Token Factory base URL is invalid")
        self._transport = transport
        self._timeout = httpx.Timeout(connect=CONNECT_TIMEOUT_SECONDS,
                                      read=_read_timeout_seconds(read_timeout_seconds),
                                      write=WRITE_TIMEOUT_SECONDS, pool=POOL_TIMEOUT_SECONDS)
        self._retries = retries
        if retries < 0 or retries > 2:
            raise ValueError("retries must be between zero and two")

    def complete_json(self, system: str, user: str, *, max_output_tokens: int = 800,
                      reasoning_policy: ReasoningPolicy = ReasoningPolicy.PROVIDER_DEFAULT) -> ModelResponse:
        validate_output_budget(max_output_tokens)
        if type(reasoning_policy) is not ReasoningPolicy:
            raise ValueError("reasoning_policy must be a trusted ReasoningPolicy")
        if not self._api_key:
            raise ModelError("missing_api_key", "NEBIUS_API_KEY is required")
        payload = {"model": self.model_id, "messages": [{"role": "system", "content": system},
                   {"role": "user", "content": user}], "response_format": {"type": "json_object"},
                   "temperature": 0, "max_tokens": max_output_tokens}
        if reasoning_policy is ReasoningPolicy.DISABLED:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        started = monotonic()
        for attempt in range(self._retries + 1):
            try:
                with httpx.Client(transport=self._transport, timeout=self._timeout, trust_env=False) as client:
                    with client.stream("POST", self._base_url + "/chat/completions",
                                       headers={"Authorization": "Bearer " + self._api_key}, json=payload) as response:
                        status = response.status_code
                        content_type = response.headers.get("content-type", "")
                        if status == 429 or status >= 500:
                            if attempt < self._retries:
                                continue
                            raise ModelError("provider_unavailable", f"Token Factory returned HTTP {status}")
                        if status >= 400:
                            raise ModelError("provider_error", f"Token Factory returned HTTP {status}")
                        chunks = []
                        size = 0
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > MAX_RESPONSE_BYTES:
                                raise _safe_response_error("response_too_large", status, content_type)
                            chunks.append(chunk)
                try:
                    body = json.loads(b"".join(chunks), object_pairs_hook=_unique_pairs,
                                      parse_constant=_reject_constant)
                except (ValueError, UnicodeDecodeError):
                    raise _safe_response_error("response_json_invalid", status, content_type) from None
                if not isinstance(body, dict):
                    raise _safe_response_error("response_shape", status, content_type)
                choices = body.get("choices")
                if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                    raise _safe_response_error("response_shape", status, content_type, body)
                choice = choices[0]
                message = choice.get("message")
                if not isinstance(message, dict):
                    raise _safe_response_error("response_shape", status, content_type, body, choice)
                if choice.get("finish_reason") == "length":
                    raise _safe_response_error("truncated_output", status, content_type, body, choice, message)
                if choice.get("finish_reason") != "stop":
                    raise _safe_response_error("incomplete_output", status, content_type, body, choice, message)
                if message.get("refusal"):
                    raise _safe_response_error("model_refusal", status, content_type, body, choice, message)
                try:
                    data = parse_one_json_object(message.get("content"))
                except ContentParseError as exc:
                    raise _safe_response_error(exc.category, status, content_type, body, choice, message) from None
                prompt_tokens, completion_tokens, reasoning_tokens = _usage_counts(body)
                return ModelResponse(data, ModelUsage(self.model_id, prompt_tokens,
                                                      completion_tokens, monotonic() - started,
                                                      attempt + 1, reasoning_tokens))
            except httpx.TimeoutException as exc:
                if attempt >= self._retries:
                    kind = ("connect_timeout" if isinstance(exc, httpx.ConnectTimeout) else
                            "read_timeout" if isinstance(exc, httpx.ReadTimeout) else
                            "write_timeout" if isinstance(exc, httpx.WriteTimeout) else
                            "pool_timeout" if isinstance(exc, httpx.PoolTimeout) else "timeout")
                    raise ModelError(kind, f"Token Factory request timed out ({kind})") from None
            except httpx.RequestError:
                if attempt >= self._retries:
                    raise ModelError("network_error", "Token Factory request failed") from None
        raise AssertionError("unreachable retry state")
