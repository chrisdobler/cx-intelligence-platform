# Golden Evaluation Dataset

Version-controlled ground truth for `app evaluate` (Phase 7, ADR-015). One JSON
file per case under `understanding/`, `retrieval/`, and `resolution/`;
`dataset.json` carries the dataset version. Every file is validated against
`cxintel.evaluation.golden` on load — unknown fields are rejected.

## Authoring workflow

1. Pick a source conversation from `data/raw/sample_tickets_v6.json` (or the
   database). **Retrieval and resolution cases must reference conversations
   covered by the derived data snapshot** (`data/processed/data-artifacts.tgz`)
   so the knowledge base contains their documents — expected results use the
   conversation `external_id` (`conv_xxxxxxxx`), the only identifier stable
   across database rebuilds.
2. Write only the fields you are confident about. **Every omitted field is
   simply not checked.** Free-form prose (summaries, reasoning,
   recommendations) is never comparable and has no expectation fields.
3. Prefer allowed-set assertions over exact ones where legitimate ambiguity
   exists: `severity_in`, `canonical_name_aliases` (the issue catalog contains
   near-synonym categories), `evidence_strength_in`.
4. Give the file a readable name (`und-001-base-water-leak.json`) — files are
   loaded in filename order and `case_id` must be globally unique.
5. Validate without touching the database or the LLM:

   ```bash
   uv run app evaluate --check
   ```

## Case shapes

- **understanding/**: transcript in → constraints on the extracted
  `StructuredConversation` (issue presence by canonical name + aliases,
  severity/impact sets, resolution booleans, keyword checks on symptoms).
- **retrieval/**: a canonical `Issue` in → expected source conversation
  external ids in the top-k, `min_recall`, optional `expect_filter_relaxed`,
  and optional explicitly enumerated acceptable document refs for semantically
  equivalent KnowledgeDocuments outside the source-id pool.
- **resolution/**: a canonical `Issue` in → constraints on the validated
  `ResolutionResponse` (grounded flag, evidence strength set, citation count
  bounds and provenance, action keywords).

## Baseline

`evals/baseline/evaluation-baseline.json` is the committed regression
baseline — a previously promoted report. After reviewing a good run, promote
it and commit the result:

```bash
uv run app evaluate --promote-baseline
```
