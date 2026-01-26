"""MCP Host for managing client instances and handling interactions."""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, cast

from dotenv import load_dotenv
from openai import AsyncOpenAI, OpenAIError

from .client import MCPHTTPClient

# Load environment variables
load_dotenv()

LOGGER = logging.getLogger("mash.mcp.host")

Role = Literal["assistant", "system", "user"]
StopReason = Literal["endTurn", "stopSequence", "maxTokens"]


@dataclass
class SamplingTextContent:
    """Typed representation of text-only sampling content."""

    text: str


@dataclass
class SamplingMessageSchema:
    """Typed schema for sampling messages per the 2025-11-25 spec."""

    role: Role
    contents: List[SamplingTextContent] = field(default_factory=list)

    @property
    def text(self) -> str:
        parts = [content.text.strip() for content in self.contents if content.text]
        return "\n".join(part for part in parts if part)

    @classmethod
    def from_raw(cls, payload: Any) -> Optional["SamplingMessageSchema"]:
        if not isinstance(payload, dict):
            return None
        role = str(payload.get("role") or "user").strip().lower()
        if role not in {"assistant", "system", "user"}:
            role = "user"
        contents = cls._parse_contents(payload.get("content"))
        if not contents:
            return None
        return cls(role=cast(Role, role), contents=contents)

    @staticmethod
    def _parse_contents(content: Any) -> List[SamplingTextContent]:
        if content is None:
            return []
        if isinstance(content, str):
            text = content.strip()
            return [SamplingTextContent(text=text)] if text else []
        if isinstance(content, dict):
            text = SamplingMessageSchema._extract_text(content)
            return [SamplingTextContent(text=text)] if text else []
        if isinstance(content, list):
            parsed: List[SamplingTextContent] = []
            for entry in content:
                if isinstance(entry, str):
                    cleaned = entry.strip()
                    if cleaned:
                        parsed.append(SamplingTextContent(text=cleaned))
                elif isinstance(entry, dict):
                    text = SamplingMessageSchema._extract_text(entry)
                    if text:
                        parsed.append(SamplingTextContent(text=text))
            return parsed
        return []

    @staticmethod
    def _extract_text(content: Dict[str, Any]) -> str:
        if content.get("type") == "text" and isinstance(content.get("text"), str):
            return content["text"].strip()
        if isinstance(content.get("text"), str):
            return str(content["text"]).strip()
        return ""


@dataclass
class ModelHintSchema:
    """Hint object for selecting an appropriate model."""

    name: str

    @classmethod
    def from_raw(cls, raw: Any) -> Optional["ModelHintSchema"]:
        if not isinstance(raw, dict):
            return None
        name = str(raw.get("name") or "").strip()
        if not name:
            return None
        return cls(name=name)


@dataclass
class ModelPreferencesSchema:
    """Subset of model preferences relevant to the host."""

    hints: List[ModelHintSchema] = field(default_factory=list)
    cost_priority: Optional[float] = None
    speed_priority: Optional[float] = None
    intelligence_priority: Optional[float] = None

    @classmethod
    def from_raw(cls, raw: Any) -> "ModelPreferencesSchema":
        if not isinstance(raw, dict):
            return cls()
        hints: List[ModelHintSchema] = []
        raw_hints = raw.get("hints")
        if isinstance(raw_hints, list):
            for hint in raw_hints:
                parsed = ModelHintSchema.from_raw(hint)
                if parsed:
                    hints.append(parsed)
        return cls(
            hints=hints,
            cost_priority=_coerce_float(raw.get("costPriority")),
            speed_priority=_coerce_float(raw.get("speedPriority")),
            intelligence_priority=_coerce_float(raw.get("intelligencePriority")),
        )


@dataclass
class SamplingRequestSchema:
    """Typed view of a sampling/createMessage request."""

    sampling_id: str
    system_prompt: Optional[str]
    messages: List[SamplingMessageSchema]
    max_tokens: int
    temperature: Optional[float]
    stop_sequences: List[str]
    metadata: Dict[str, Any]
    model_preferences: ModelPreferencesSchema

    @classmethod
    def from_payload(
        cls,
        payload: Dict[str, Any],
        *,
        sampling_id: str,
        max_tokens: int,
    ) -> "SamplingRequestSchema":
        system_prompt = _clean_string(payload.get("systemPrompt"))
        raw_messages = payload.get("messages")
        if not isinstance(raw_messages, list):
            raise ValueError("Sampling request messages must be provided as a list.")
        messages: List[SamplingMessageSchema] = []
        for entry in raw_messages:
            parsed = SamplingMessageSchema.from_raw(entry)
            if parsed:
                messages.append(parsed)
        if not messages and not system_prompt:
            raise ValueError("Sampling requests must include textual content.")
        temperature = _coerce_float(payload.get("temperature"))
        stop_sequences = _coerce_stop_sequences(payload.get("stopSequences"))
        raw_metadata = payload.get("metadata")
        metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
        return cls(
            sampling_id=sampling_id,
            system_prompt=system_prompt,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            stop_sequences=stop_sequences,
            metadata=metadata,
            model_preferences=ModelPreferencesSchema.from_raw(
                payload.get("modelPreferences")
            ),
        )

    def preview_excerpt(self, limit: int = 2000) -> str:
        for message in reversed(self.messages):
            text = message.text
            if text:
                return text[:limit]
        return (self.system_prompt or "")[:limit]

    def to_openai_messages(self) -> List[Dict[str, str]]:
        messages: List[Dict[str, str]] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        for message in self.messages:
            text = message.text
            if text:
                messages.append({"role": message.role, "content": text})
        return messages


@dataclass
class SamplingResponseSchema:
    """Typed structure for returning sampling responses."""

    sampling_id: str
    role: Role
    model: str
    text: str
    stop_reason: StopReason | str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "samplingId": self.sampling_id,
            "role": self.role,
            "model": self.model,
            "content": {"type": "text", "text": self.text},
            "stopReason": self.stop_reason,
        }


@dataclass
class OpenAIChatResult:
    """Normalized result returned from the OpenAI chat completion API."""

    role: Role
    model: str
    text: str
    finish_reason: Optional[str]


def _clean_string(value: Any) -> Optional[str]:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _coerce_float(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        if isinstance(value, str) and value.strip():
            return float(value)
    except ValueError:
        return None
    return None


def _coerce_stop_sequences(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    stops: List[str] = []
    for entry in value:
        if isinstance(entry, str):
            cleaned = entry.strip()
            if cleaned:
                stops.append(cleaned)
    return stops


def _map_finish_reason(reason: Optional[str]) -> StopReason | str:
    mapping = {
        None: "endTurn",
        "stop": "stopSequence",
        "length": "maxTokens",
        "content_filter": "contentFilter",
        "max_tokens": "maxTokens",
    }
    return mapping.get(reason, reason or "endTurn")


class Host:
    """Host process that manages MCP client instances and interactions.

    The host is responsible for creating/tearing down clients, mediating
    sampling/elicitation requests, and enforcing basic policies before
    handing data off to humans or downstream LLMs.
    """

    def __init__(self, default_model: Optional[str] = None) -> None:
        self._clients: Dict[str, MCPHTTPClient] = {}
        self._default_model = (
            default_model
            or os.environ.get("MASH_SAMPLING_MODEL")
            or os.environ.get("PLOG_SAMPLING_MODEL")
            or "gpt-4.1-mini"
        )

    def get_client(
        self, url: str, name: str, headers: Optional[Dict[str, str]] = None
    ) -> MCPHTTPClient:
        """Return a singleton MCP client for the given server URL."""
        normalized_headers = (
            {
                str(key): str(value)
                for key, value in headers.items()
                if isinstance(key, str) and isinstance(value, str)
            }
            if headers
            else None
        )
        cache_key = self._client_cache_key(url, normalized_headers)
        client = self._clients.get(cache_key)
        if client is not None:
            return client
        client = MCPHTTPClient(
            url,
            client_name=name,
            default_headers=normalized_headers,
            sampling_handler=self._handle_sampling_request,
            elicitation_handler=self._handle_elicitation_request,
        )
        self._clients[cache_key] = client
        return client

    def _client_cache_key(self, url: str, headers: Optional[Dict[str, str]]) -> str:
        if not headers:
            return url
        header_blob = "|".join(
            f"{key}:{value}" for key, value in sorted(headers.items())
        )
        return f"{url}|{header_blob}"

    def close(self) -> None:
        """Shut down all managed MCP client connections."""
        for client in self._clients.values():
            client.close()
        self._clients.clear()

    # ------------------------------------------------------------------
    # Interaction handling
    # ------------------------------------------------------------------
    async def _handle_sampling_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Handle sampling by invoking the configured OpenAI model."""

        sampling_id = str(request.get("samplingId") or uuid.uuid4())
        LOGGER.info("sampling request received: %s", sampling_id)
        try:
            sampling_payload = SamplingRequestSchema.from_payload(
                request,
                sampling_id=sampling_id,
                max_tokens=self._sanitize_max_tokens(request.get("maxTokens")),
            )
            preview = sampling_payload.preview_excerpt()
            if preview:
                LOGGER.info("sampling prompt excerpt:\n%s", preview)
            openai_messages = sampling_payload.to_openai_messages()
            if not openai_messages:
                raise RuntimeError("Sampling request did not include any text content.")
            model_name = self._select_model(sampling_payload)
            completion = await self._perform_openai_chat(
                openai_messages=openai_messages,
                model_name=model_name,
                sampling_request=sampling_payload,
            )
            LOGGER.info(
                "sampling request %s completed with model %s",
                sampling_id,
                completion.model,
            )
            response = SamplingResponseSchema(
                sampling_id=sampling_payload.sampling_id,
                role=completion.role,
                model=completion.model,
                text=completion.text,
                stop_reason=_map_finish_reason(completion.finish_reason),
            )
            return response.to_dict()
        except (OpenAIError, RuntimeError, ValueError) as exc:
            LOGGER.error("sampling error invoking OpenAI: %s", exc)
            error_text = f"(sampling error) {exc}"
        except Exception as exc:  # pylint: disable=broad-except
            LOGGER.error("sampling unexpected error: %s", exc)
            error_text = f"(sampling error) {exc}"
        return SamplingResponseSchema(
            sampling_id=sampling_id,
            role="assistant",
            model="simulated-mash-llm",
            text=error_text,
            stop_reason="endTurn",
        ).to_dict()

    def _handle_elicitation_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Prompt the operator for more input when servers request it."""

        question = (
            request.get("message")
            or request.get("prompt")
            or "Server requested additional input"
        )
        print(f"[elicitation] {question}")
        answer = input("> ")
        return {
            "elicitationId": request.get("elicitationId") or str(uuid.uuid4()),
            "response": answer,
        }

    def _select_model(self, request: SamplingRequestSchema) -> str:
        """Pick a model name based on server hints or defaults."""

        for hint in request.model_preferences.hints:
            if hint.name:
                return hint.name
        hinted = request.metadata.get("model") or request.metadata.get("name")
        if isinstance(hinted, str) and hinted.strip():
            return hinted.strip()
        return self._default_model

    async def _perform_openai_chat(
        self,
        *,
        openai_messages: List[Dict[str, str]],
        model_name: str,
        sampling_request: SamplingRequestSchema,
    ) -> OpenAIChatResult:
        """Call the AsyncOpenAI chat endpoint."""

        params: Dict[str, Any] = {
            "model": model_name,
            "messages": openai_messages,
            "max_tokens": sampling_request.max_tokens,
        }
        if sampling_request.temperature is not None:
            params["temperature"] = sampling_request.temperature
        if sampling_request.stop_sequences:
            params["stop"] = sampling_request.stop_sequences
        async with AsyncOpenAI() as client:
            response = await client.chat.completions.create(**params)
        choice = response.choices[0]
        text = self._extract_choice_text(choice.message.content)
        return OpenAIChatResult(
            role=getattr(choice.message, "role", None) or "assistant",
            model=response.model,
            text=text.strip(),
            finish_reason=choice.finish_reason,
        )

    @staticmethod
    def _sanitize_max_tokens(value: Any) -> int:
        """Clamp the requested token count to a reasonable range."""

        try:
            tokens = int(value)
        except (TypeError, ValueError):
            tokens = 512
        return max(1, min(tokens, 4096))

    def _extract_choice_text(self, content: Any) -> str:
        """Normalize the assistant message content to a plain string."""

        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            fragments: List[str] = []
            for block in content:
                if isinstance(block, str):
                    fragments.append(block)
                elif isinstance(block, dict):
                    text = block.get("text")
                    if isinstance(text, str):
                        fragments.append(text)
            return "\n".join(fragments)
        if hasattr(content, "text"):
            return getattr(content, "text")
        return str(content)


__all__ = ["Host"]
