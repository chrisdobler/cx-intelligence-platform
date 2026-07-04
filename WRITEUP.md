

# Conversation Intelligence Platform

> This document will become the final submission write-up. Keep it under two pages of text. Populate sections as implementation progresses.

## 1. Problem
- What problem is being solved?
- Why this architecture?

## 2. Overall Architecture
- Final architecture diagram (reference ARCHITECTURE.md)
- Brief pipeline overview

## 3. Part 1 — Clustering & Anomaly Detection
- Approach
- Design decisions
- Results
- Tradeoffs

### Canonical Issue Classification

One of the primary design challenges was preventing semantically identical
customer problems from fragmenting into many slightly different issue names.

Rather than treating issue extraction as a free-form naming task, the
Conversation Understanding prompt frames the problem as **operational
classification**.

The prompt receives the Day 1 Issue Catalog and classifies each extracted issue
into broad, stable operational categories whenever appropriate while preserving
the customer's original wording separately.

This significantly reduces taxonomy fragmentation and produces issue categories
that are suitable for reporting, anomaly detection, and long-term trend
analysis.

The implementation intentionally delegates canonicalization to the LLM for
Version 1. A dedicated taxonomy service would likely be appropriate for a
larger production deployment but was intentionally deferred to keep the
architecture simple while still preserving a clear evolution path.

## 4. Part 2 — Resolution Assistant
- KnowledgeDocument generation
- Deterministic knowledge synthesis
- Knowledge base
- Retrieval strategy
- Context engineering
- Resolution generation

### Grounded Recommendations

The Resolution Assistant is intentionally grounded in retrieved historical
knowledge rather than the model's general knowledge.

The assistant receives a deterministic ContextBundle containing the current
issue and the most relevant historical KnowledgeDocuments.

Recommendations are produced only from that evidence and include citations to
the supporting KnowledgeDocuments.

When no sufficiently similar historical resolutions exist, the assistant
explicitly reports that no grounded recommendation can be made rather than
inventing troubleshooting guidance.

### Grounding Enforced in Code

Grounding is a platform guarantee, not a prompt aspiration. The context
builder assigns each retrieved KnowledgeDocument a stable citation id
(`KB-1`, `KB-2`, … in retrieval rank order), and every LLM response passes a
deterministic validation step: citations that do not reference a supplied
document are dropped, a response claiming to be grounded while citing no
retrieved document is downgraded to ungrounded, and an ungrounded response
cannot carry recommended actions. When retrieval returns nothing at all, the
platform answers deterministically without invoking the LLM — "no evidence"
costs no tokens and cannot hallucinate.

The retrieval query itself is rendered deterministically from the selected
issue using the same field labels as the embedded `knowledge_text`, so query
and documents occupy the same embedding space. The current issue is taken
verbatim from the Structured Conversation Object — the assistant never
reinterprets conversations (a new free-text ticket is structured once by the
existing Prompt #1 and is not persisted).

## 5. Key Engineering Decisions
- Reference DESIGN_DECISIONS.md
- Summarize only the most important decisions.

## 6. Future Improvements
- Tool calling
- Evaluation
- Reranking
- Monitoring
