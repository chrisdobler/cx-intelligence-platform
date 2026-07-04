"""Tests for Prompt #1 assembly and documentation sync."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

from cxintel.models import Conversation, IssueCatalogEntry, Message
from cxintel.understanding.prompt import PROMPT_VERSION, build_prompt

REPO_ROOT = Path(__file__).resolve().parents[1]


def _conversation() -> Conversation:
    return cast(
        Conversation,
        SimpleNamespace(
            product="Pod 4",
            category="hardware",
            priority="medium",
            status="open",
        ),
    )


def _messages() -> list[Message]:
    return [
        cast(
            Message,
            SimpleNamespace(
                role="customer",
                body="My pod 4 keeps getting too hot after the firmware update.",
            ),
        )
    ]


def _catalog() -> list[IssueCatalogEntry]:
    return [
        cast(
            IssueCatalogEntry,
            SimpleNamespace(
                canonical_name="pod overheating",
                description="Pod temperature exceeds the target range.",
            ),
        )
    ]


def _prompt() -> str:
    return build_prompt(_conversation(), _messages(), _catalog())


def _documented_prompt_1() -> str:
    doc = (REPO_ROOT / "docs" / "PROMPT_LIBRARY.md").read_text()
    prompt_start = doc.index("```text\n") + len("```text\n")
    prompt_end = doc.index("\n```", prompt_start)
    return doc[prompt_start:prompt_end]


def _documented_catalog_placeholder() -> str:
    documented = _documented_prompt_1()
    placeholder_start = documented.index("{catalog block")
    placeholder_end = documented.index("}\n\nTreat issue", placeholder_start) + 1
    return documented[placeholder_start:placeholder_end]


def test_prompt_version_bumped_for_canonicalization_guidance() -> None:
    assert PROMPT_VERSION == "1.1"


def test_prompt_treats_issue_extraction_as_classification() -> None:
    prompt = _prompt()

    assert "Treat issue extraction as a classification task rather than a naming task." in prompt
    assert "classify customer problems into stable operational" in prompt
    assert "reporting categories" in prompt


def test_prompt_guides_catalog_reuse_and_customer_wording_separation() -> None:
    prompt = _prompt()

    assert "Reuse an existing catalog category whenever it accurately represents" in prompt
    assert "Reuse the catalog's exact canonical_name when matched." in prompt
    assert "Preserve the customer's original wording separately" in prompt
    assert "customer_description" in prompt
    assert "Avoid creating a new canonical category when an existing category" in prompt
    assert "appropriate fit" in prompt
    assert "firmware revisions, or product revisions" in prompt


def test_prompt_defines_stable_reporting_oriented_canonical_names() -> None:
    prompt = _prompt()

    assert "Canonical issue names should be:" in prompt
    assert "- short" in prompt
    assert "- lowercase" in prompt
    assert "- stable over time" in prompt
    assert "- appropriate for reporting and analytics" in prompt
    assert "- independent of customer wording whenever practical" in prompt


def test_prompt_library_prompt_1_matches_runtime_instruction_text() -> None:
    prompt = _prompt()
    runtime_instruction_text = prompt.split("Conversation metadata:", maxsplit=1)[0].strip()
    runtime_catalog_block = (
        "The current issue catalog (the platform's known issue taxonomy):\n"
        "- pod overheating: Pod temperature exceeds the target range."
    )
    runtime_with_documented_placeholder = runtime_instruction_text.replace(
        runtime_catalog_block,
        _documented_catalog_placeholder(),
    )

    documented_instruction_text = _documented_prompt_1().split(
        "Conversation metadata:", maxsplit=1
    )[0].strip()

    assert documented_instruction_text == runtime_with_documented_placeholder


def test_prompt_library_documents_prompt_1_version() -> None:
    doc = (REPO_ROOT / "docs" / "PROMPT_LIBRARY.md").read_text()

    assert "## Prompt 1" in doc
    assert '_Status: Implemented (`prompt_version = "1.1"`' in doc
