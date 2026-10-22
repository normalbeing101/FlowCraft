"""Provider adapter interface — model choice lives in deployment config,
never hardcoded in workflow definitions."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ChatMessage:
    role: str  # system | user | assistant
    content: str


@dataclass
class GenerationRequest:
    messages: list[ChatMessage]
    model: str | None = None            # None -> provider profile default
    temperature: float | None = None
    max_output_tokens: int | None = None
    timeout_seconds: float | None = None
    json_schema: dict | None = None     # structured-output requirement
    extra: dict = field(default_factory=dict)


@dataclass
class GenerationResult:
    text: str
    model_used: str
    provider: str
    usage: dict | None = None           # ONLY provider-reported usage; never estimated
    capability_notes: list[str] = field(default_factory=list)


@dataclass
class ProviderCapabilities:
    chat: bool = True
    structured_output: bool = False      # native JSON-schema mode
    temperature: bool = True
    max_output_tokens: bool = True


class ProviderAdapter(ABC):
    """Minimal, honest interface. Adapters must classify failures using the
    engine's FailureClass taxonomy and must NOT invent usage numbers."""

    name: str = "abstract"

    def __init__(self, capabilities: ProviderCapabilities) -> None:
        self.capabilities = capabilities

    @abstractmethod
    async def generate(self, req: GenerationRequest) -> GenerationResult: ...

    def validate_request(self, req: GenerationRequest) -> list[str]:
        """Return human-readable warnings for unsupported features (never
        silently pretend a capability exists)."""
        notes: list[str] = []
        if req.json_schema and not self.capabilities.structured_output:
            notes.append(
                f"provider {self.name!r} has no native structured output; "
                "schema will be enforced client-side with validation + one repair attempt"
            )
        if req.temperature is not None and not self.capabilities.temperature:
            notes.append(f"provider {self.name!r} does not support temperature; value ignored")
        if req.max_output_tokens and not self.capabilities.max_output_tokens:
            notes.append(f"provider {self.name!r} does not support max_output_tokens; value ignored")
        return notes
