"""Tests for Prompt #2 assembly, grounding rules, and documentation sync."""

from __future__ import annotations

import uuid
from pathlib import Path

from cxintel.knowledge_base.schema import KnowledgeDocument
from cxintel.resolution_assistant.prompt import (
    _INSTRUCTIONS,
    PROMPT_VERSION,
    build_resolution_prompt,
)
from cxintel.resolution_assistant.schema import (
    ContextBundle,
    ContextDocument,
    RetrievalMetadata,
)

from .test_knowledge_generation import make_issue

REPO_ROOT = Path(__file__).resolve().parents[1]


def _bundle() -> ContextBundle:
    return ContextBundle(
        issue=make_issue("base water leak"),
        documents=[
            ContextDocument(
                doc_id="KB-1",
                conversation_id=uuid.uuid5(uuid.NAMESPACE_URL, "kb_leak"),
                distance=0.12,
                document=KnowledgeDocument(
                    issue="base water leak",
                    product="Pod 5",
                    symptoms=["water pooling under the base"],
                    resolution_type="replacement",
                    resolution_summary="replaced the base seal",
                    actions=["shipped a replacement base"],
                    outcome="resolved",
                ),
            )
        ],
        retrieval=RetrievalMetadata(
            query_text="Problem:\nbase water leak",
            product_filter="Pod 5",
            filter_relaxed=False,
            limit=5,
            result_count=1,
            distances=[0.12],
        ),
    )


def test_prompt_version() -> None:
    assert PROMPT_VERSION == "1.0"


def test_prompt_embeds_the_bundle_as_json_payload() -> None:
    prompt = build_resolution_prompt(_bundle())
    assert '"doc_id": "KB-1"' in prompt
    assert '"canonical_name": "base water leak"' in prompt
    assert '"query_text": "Problem:\\nbase water leak"' in prompt


def test_prompt_states_the_grounding_rules() -> None:
    prompt = build_resolution_prompt(_bundle())
    assert "Use ONLY the knowledge documents in the bundle" in prompt
    assert "never invent" in prompt
    assert "troubleshooting steps that no cited document contains" in prompt
    assert "No sufficiently similar historical resolutions were found." in prompt
    assert "Do not reinterpret, re-diagnose, or second-guess the current issue" in prompt
    assert 'citations must be the doc_id values (e.g. "KB-1")' in prompt
    assert "An honest ungrounded answer is" in prompt


def test_prompt_defines_evidence_strength_semantics() -> None:
    prompt = build_resolution_prompt(_bundle())
    for level in ('"strong"', '"moderate"', '"weak"', '"none"'):
        assert level in prompt


def _documented_prompt_2() -> str:
    doc = (REPO_ROOT / "docs" / "PROMPT_LIBRARY.md").read_text()
    section_start = doc.index("## Prompt 2")
    prompt_start = doc.index("```text\n", section_start) + len("```text\n")
    prompt_end = doc.index("\n```", prompt_start)
    return doc[prompt_start:prompt_end]


def test_prompt_library_prompt_2_matches_runtime_instruction_text() -> None:
    assert _documented_prompt_2() == _INSTRUCTIONS.rstrip("\n")


def test_prompt_library_documents_prompt_2_version() -> None:
    doc = (REPO_ROOT / "docs" / "PROMPT_LIBRARY.md").read_text()
    assert "## Prompt 2" in doc
    assert f'_Status: Implemented (`prompt_version = "{PROMPT_VERSION}"`' in doc
