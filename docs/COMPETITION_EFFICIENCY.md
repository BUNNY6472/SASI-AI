# Competition ideas adapted for SASI

Research and implementation: 18 October 2026. These changes adapt techniques to SASI's existing API workflow. They do not reproduce competition systems, train a new model, or establish a leaderboard score.

## Sources and decisions

| Primary source | Relevant lesson | SASI adaptation |
|---|---|---|
| [Kaggle LLM Science Exam, 3rd place](https://www.kaggle.com/competitions/kaggle-llm-science-exam/writeups/podpall-3rd-place-solution-update-code-links) | Passage retrieval and reranking matter; information outside article introductions must remain discoverable. | Rank passages throughout the available preview, including late passages, instead of favoring the beginning or counting repeated keywords. |
| [Kaggle LLM Science Exam, 6th place](https://www.kaggle.com/c/kaggle-llm-science-exam/discussion/447647) | Retrieve candidates, rerank, then give the reader a small relevant set; neighboring context helps interpretation. | Select a bounded set of passages, retain surrounding context and source ranges, and avoid repeating exact duplicate text. |
| [KDD Cup 2024 CRAG, winning paper](https://arxiv.org/html/2410.00005v1) | Small retrieval units can identify relevant information while larger parent passages retain context. Retrieval and reranking share a strict context/time budget. | Share one excerpt allowance across all retrieved sources at each stage, keep every source's provenance, and retain context around relevant passages. |
| [AIMO-2 winning paper](https://arxiv.org/abs/2504.16891), [NVIDIA team account](https://blogs.nvidia.com/blog/reasoning-ai-math-olympiad/), [second-place authors' implementation](https://github.com/imagination-research/aimo2) | Allocate inference effort dynamically and avoid continuing redundant candidates once a narrow stopping condition is met. | Stop scheduling repeated reviews/fixes when a completed revision returns exactly the same rejected artifact under unchanged verification context. Keep rejection findings and explicit limitations. |

The retrieval implementation uses deterministic CPU scoring, including term rarity and frequency saturation. It is a resource-conscious adaptation of the retrieve/rerank/select pattern, **not** the competition solutions' trained embedding models or cross-encoders. Exact duplicate elimination is deliberately conservative: changed numbers, negation and conflicting claims remain distinct.

The AIMO competition's integer-answer agreement test does not establish correctness for open-ended research. SASI therefore retains independent review and source checks; it does not accept an answer because several models repeat it. The NVIDIA paper's GenSelect research was not used in its winning Kaggle submission. GPU quantization, speculative decoding and competition-scale model training are not implemented in this API application.

## Implemented behavior

- File preview selection retains source identities, character ranges, opening context and relevant constraints. It can select fewer passages than the maximum when the query has clear matches. A query without useful matches receives a coverage-oriented fallback with explicit omissions.
- Retrieved evidence has one shared excerpt budget per stage: 12,000–24,000 characters. Small excerpts stay complete where possible. All source IDs, URLs, retrieval times, source kinds and original truncation flags remain available. Identical excerpts are supplied once with source aliases; aliases do not imply independent corroboration.
- Task revisions that reproduce an already rejected artifact do not purchase another identical review in the same unchanged context. A bounded alternative implementer can still try. Unchanged final fixes stop further redundant repair/review rounds and retain failure findings. Persisted stopping state is invalidated when the artifact or verification context changes.
- Full user instructions, requested output constraints, deliverables under review and accepted dependency artifacts are not compressed by these changes. Stored originals and saved source excerpts are not mutated.
- The 5,000,000 aggregate-token ceiling, owner access gate and existing trial routing remain unchanged. Active provider calls retain their timing policy; efficiency decisions happen between completed calls. At the user's subsequent request, the resumable upload allowance is increased to 10 GiB combined (also the per-file maximum), with up to 20 files.
- ZIP archives above the bounded preview size are stored without loading or unpacking their members. Their coverage explicitly says that only the archive name and size were supplied to models. Relevant extracted files or a schema/manifest are needed for content analysis; accepting a large archive is not equivalent to inspecting its contents.

## Evaluation boundary

Offline regression cases cover late relevant facts, rare identifiers, repeated-keyword distractors, changed numeric values, negation, duplicate provenance, no-match fallback, source-budget bounds, unchanged failed revisions and changed-context revalidation. These measure selection and controller behavior without paid model calls.

Measured offline fixtures: a 155,210-character report used 23,600 context characters with the prior selector and 2,993 with the new selector, retaining all three planted facts/constraints. Eight identical previews shrank from 43,048 to 16,879 context characters while retaining all eight source IDs. A separate five-source evidence fixture retained all five late target facts in 7,318 rendered context characters from 120,120 stored excerpt characters. These are synthetic context-size measurements, not billed-token savings or answer-quality scores.

Passing those cases is not evidence of a general answer-quality improvement or a measured bill reduction. Use the existing `evals/run_eval.py` harness with matched tasks, models and budgets for a separately authorized live comparison, and use blinded human grading for open-ended research. Record provider usage and cache hits alongside quality; do not compare uncalibrated model self-scores as if they were competition results.
