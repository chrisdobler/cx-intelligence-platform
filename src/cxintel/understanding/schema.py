"""The canonical Structured Conversation Object — Version 1 (frozen).

This Pydantic hierarchy is the single source of truth for the contract between
Conversation Understanding and every downstream component (see
docs/PHASE3-UNDERSTANDING.md). It serves four roles at once:

- runtime validation of every LLM response,
- native structured-output schema generation for the provider,
- the application-level types the rest of the platform consumes,
- the exact shape persisted (unchanged) in ``ConversationAnalysis.analysis_json``.

Do not duplicate this schema anywhere — prompts define semantics, this module
defines structure. Extend conservatively; never redesign (V1 is frozen).

Field descriptions matter: they flow into the provider-generated JSON Schema
and are the only structure-adjacent guidance the model receives.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["low", "medium", "high", "critical"]
CustomerImpact = Literal["low", "medium", "high"]
ResolutionStatus = Literal["resolved", "in_progress", "unresolved", "escalated"]


class Summary(BaseModel):
    """Conversation summaries at two levels of detail."""

    short: str = Field(description="One-sentence summary of the conversation.")
    detailed: str = Field(
        description="A few sentences covering the problem(s), key events, and outcome."
    )


class CatalogMatch(BaseModel):
    """Whether the issue was normalized against the current issue catalog."""

    matched: bool = Field(
        description=(
            "True when the issue was matched to an existing catalog category; "
            "false when this is a new canonical issue not in the catalog."
        )
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence in the catalog match decision, from 0 to 1.",
    )


class Issue(BaseModel):
    """One operational issue discussed in the conversation.

    The Issue — not the conversation — is the platform's fundamental
    analytical unit. Each Issue is projected 1:1 into ``conversation_issues``.
    """

    canonical_name: str = Field(
        description=(
            "Short, normalized, lowercase name for the issue category "
            "(e.g. 'base water leak'). Reuse a catalog category name when one fits."
        )
    )
    customer_description: str = Field(
        description="The customer's own wording for the problem, preserved verbatim."
    )
    severity: Severity = Field(description="Operational severity of the issue.")
    confidence: float = Field(
        ge=0.0, le=1.0, description="Confidence that this issue was correctly identified."
    )
    customer_impact: CustomerImpact = Field(
        description="How strongly the issue impacts the customer's use of the product."
    )
    product: str = Field(description="The product the issue concerns (e.g. 'Pod 5').")
    symptoms: list[str] = Field(
        description="Concrete symptoms or evidence quoted or paraphrased from the conversation."
    )
    catalog: CatalogMatch = Field(
        description="Normalization result against the current issue catalog."
    )
    resolution_status: ResolutionStatus = Field(
        description="Resolution state of this specific issue at the end of the conversation."
    )
    resolution_summary: str | None = Field(
        description="How this issue was resolved, if it was; null otherwise."
    )


class Resolution(BaseModel):
    """The overall resolution of the conversation."""

    resolved: bool = Field(description="Whether the conversation ended resolved.")
    resolution_type: str | None = Field(
        description=(
            "Resolution category (e.g. 'replacement', 'troubleshooting'); null if unresolved."
        )
    )
    summary: str = Field(description="Concise summary of how the conversation was resolved.")
    actions: list[str] = Field(description="Concrete actions taken by the agent or customer.")
    requires_replacement: bool = Field(
        description="Whether a hardware replacement is still required after this conversation."
    )


class ConversationMeta(BaseModel):
    """Whole-conversation signals."""

    language: str = Field(description="Language of the conversation (e.g. 'English').")
    multiple_issues: bool = Field(
        description="True when the conversation covers more than one distinct issue."
    )
    requires_followup: bool = Field(
        description="True when the customer still needs a follow-up after this conversation."
    )
    customer_emotion: str = Field(
        description="Dominant customer emotion (e.g. 'frustrated', 'calm', 'angry')."
    )
    analysis_confidence: float = Field(
        ge=0.0, le=1.0, description="Overall confidence in this analysis, from 0 to 1."
    )


class StructuredConversation(BaseModel):
    """The canonical AI artifact for one conversation (Version 1, frozen)."""

    summary: Summary = Field(description="Conversation summaries.")
    issues: list[Issue] = Field(
        description="Every operational issue discussed; may legitimately be empty."
    )
    resolution: Resolution = Field(description="Overall conversation resolution.")
    conversation: ConversationMeta = Field(description="Whole-conversation signals.")
