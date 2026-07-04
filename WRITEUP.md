

# Conversation Intelligence Platform

> This document will become the final submission write-up. Keep it under two pages of text. Populate sections as implementation progresses.

## 1. Problem
- What problem is being solved?
- Why this architecture?

## 2. Overall Architecture
- Final architecture diagram (reference ARCHITECTURE.md)
- Brief pipeline overview

### Architectural Philosophy

The central architectural principle of the platform is to use LLMs only for
semantic reasoning and immediately convert their outputs into strongly typed
canonical artifacts.

Rather than allowing AI-generated text to flow through the system, every major
AI stage produces a schema-validated Pydantic model (for example,
StructuredConversation, KnowledgeDocument, or ResolutionResponse).

Once information has crossed the unstructured-to-structured boundary,
downstream processing becomes conventional software engineering.

Analytics, anomaly detection, knowledge generation, retrieval, orchestration,
and evaluation all operate deterministically on structured artifacts rather
than invoking additional LLM reasoning.

This approach improves reproducibility, simplifies testing, enables
deterministic regression evaluation, and keeps AI isolated to the parts of the
system that genuinely require semantic understanding.


### Validation During Development

One interesting observation during development was that the average number of
extracted issues per conversation increased substantially across the dataset
(approximately 1.5 → 2.8 → 3.1 issues per conversation from Day 1 through Day
3). At first this suggested either taxonomy fragmentation or duplicate issue
extraction.

Rather than changing the anomaly detection logic immediately, I validated the
underlying assumptions using deterministic SQL queries against the relational
projections. By comparing conversation counts, issue counts, and distinct
conversation/issue pairs, I confirmed that duplicate issue extraction was
negligible and that the increase reflected the structure of the dataset rather
than a defect in the parser.

This reinforced an important engineering principle used throughout the
project: when AI systems produce surprising results, validate the underlying
data and deterministic pipeline before changing model behavior.

### Taxonomy Quality as a System Property

Reviewing the anomaly output revealed that the quality of anomaly detection is
ultimately constrained by the quality of the operational taxonomy. The
multi-signal rules engine correctly identified statistically significant
changes, but semantically similar customer problems occasionally appeared under
slightly different canonical issue names (for example, variations of
temperature-control or water-leak issues).

Rather than adding increasingly complex anomaly heuristics to compensate, I
kept the detection engine intentionally simple and deterministic. This
reinforced the architectural separation of concerns: anomaly detection should
operate over a stable canonical representation, while improvements to
classification belong upstream in Conversation Understanding.

For a production system, I would evolve this through a dedicated taxonomy
service, embedding-assisted taxonomy matching, and a human approval workflow
before new categories become part of the operational catalog. Strengthening the
canonical representation improves every downstream consumer—including anomaly
detection, analytics, and retrieval—without increasing the complexity of the
rules engine.

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

Canonical anomalies are also self-contained reporting artifacts. Each anomaly
stores both the observation timestamp and the baseline timestamp directly on
the artifact, allowing reports and timeline visualizations to consume anomaly
data without reconstructing temporal context from the underlying
conversations.

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