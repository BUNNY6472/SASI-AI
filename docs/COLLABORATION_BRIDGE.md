# SASI collaboration bridge

This release changes new questions from a timed research pipeline into a bounded, question-driven conversation between connected providers. It produces one complete answer. Internal planning and feedback are short; the answer itself is not compressed for communication savings.

## Decisions from the research

| Primary source | Useful idea | SASI adaptation and boundary |
|---|---|---|
| [Kaggle LLM Science Exam, sixth-place source code](https://github.com/rbiswasfc/llm-science-exam) | Separate retriever, ranker and reader; specialize evidence before expensive reading. | Reuse CPU passage selection and exact deduplication before provider calls. The competition trained its own retriever/ranker/reader; SASI does not claim those trained models or competition results. |
| [Meta KDD Cup 2024 winning CRAG solution](https://arxiv.org/html/2410.00005v1) | Build an evidence retrieval pipeline suited to a question and its knowledge requirements. | Keep source identity, relevant excerpts, coverage and conflicting evidence available to the answerer and verifier. Retrieval is not proof that a claim is supported. |
| [AIMO2 second-place implementation](https://github.com/imagination-research/aimo2#question-level-answer-aggregation--early-stopping) | Avoid redundant generation and measure early-stopping trade-offs. | Stop after completed checks, no useful change, or the bounded correction allowance. Its majority agreement rule applies to numerical math answers; SASI does not use agreement as proof of open-ended correctness or cancel a stream upon spotting an answer. |
| [AIMO2 winning solution paper](https://arxiv.org/html/2504.16891v1) | Distinguish training, inference selection, execution and final evaluation. | Keep deterministic checks separate from model opinions. GenSelect was **not** used in the winning Kaggle submission; its later research results are not SASI results. No training, model merging or GPU quantization is represented as a hosted API setting. |
| [RouteLLM](https://arxiv.org/html/2406.18665v4) | Choose model effort using quality/cost evidence instead of asking every model. | Route among healthy, policy-allowed seats and choose bounded effort. SASI's current routing is a transparent heuristic with peer-history signals, not a trained or calibrated RouteLLM router. |
| [LLMLingua-2](https://arxiv.org/html/2403.12968v2) | Extractive selection can avoid the invented facts of free-form summaries. | Select supporting passages without rewriting their numbers, negations or provenance. Keep original instructions and the exact answer under review. The trained LLMLingua compressor is not installed. |
| [AgentPrune](https://arxiv.org/html/2410.02506v1) | Dense, repetitive communication is expensive and can spread malicious messages. | Replace report broadcasts with narrowly scoped planning and verifier packets. SASI uses a fixed, auditable topology, not AgentPrune's learned pruning algorithm. |
| [Debate Only When Necessary](https://arxiv.org/html/2504.05047) | Allocate collaboration according to the work rather than a fixed debate schedule. | A small request skips a separate planning call; harder requests can receive a compact brief. Repairs require concrete findings. Reported paper savings do not predict SASI savings. |
| [EffGen](https://arxiv.org/html/2602.00887) | Small models benefit from suitable task decomposition and compact context. | Keep the protocol provider-independent. A small model needs its own measured quality and hosting budget before becoming a trusted operational dependency. The paper also identifies routing mistakes, tool failures and tool-access confounds. |

Kaggle's dynamically rendered write-up pages were not readable through the research fetcher in this pass. The sixth-place authors' public repository, the AIMO authors' repository and the original papers above were inspected instead. These are specific relevant solutions, not a claim to have reviewed every world competition.

## Communication contract

1. The server chooses healthy connected models under the existing access and billing policy. A message cannot add a provider or change that policy.
2. The bridge creates a compact route and, when warranted, requests a short structured plan.
3. One model writes the complete answer using the original user request and scoped evidence.
4. A different connected provider reviews the complete answer and returns a small structured packet of concrete findings. A packet is not another user-facing answer.
5. A correction is requested only when findings or deterministic checks require it. A changed answer requires a new review. The workflow retains unresolved findings and incomplete verification.
6. The UI displays one answer, actual usage, sources and an expandable collaboration record.

Review payloads have strict schemas and length bounds. Unexpected keys, duplicate JSON keys, non-finite values, unknown evidence identifiers and quotations absent from the reviewed answer are rejected. A matching quotation or known source ID establishes only a structural reference, not truth.

There is no session timer for new bridge runs. Finite provider transport timeouts still handle stalled requests. Token/call caps, cancellation, trial expiry and bounded correction attempts remain enforced. Older persisted pipeline runs keep their own workflow and timing semantics.

## Security boundaries

The original user request and application instructions retain their authority. File text, website content, planner suggestions and verifier findings remain untrusted data. JSON serialization and schema checks prevent message structure from being overwritten; they do not make arbitrary text safe or eliminate semantic prompt injection.

Provider credentials stay in server-side adapters and Secret Manager. Bridge packets cannot run a shell, execute generated code, alter IAM, issue payments, choose arbitrary URLs to fetch, or edit provider configuration. Existing public-URL fetching uses address validation and redirect controls. Owner authentication protects API routes and downloads. The browser sanitizes rendered Markdown.

The answer under review is kept whole. A context limit causes safe rejection or an eligible-provider fallback rather than a passing assessment of an excerpt. Supporting attachment previews remain bounded and labelled. Review reuse is tied to the exact artifact and context; old reviews must not validate a changed answer.

This follows the containment principle in [Anthropic's engineering guidance](https://www.anthropic.com/engineering/how-we-contain-claude): capabilities and credential boundaries matter in addition to model instructions. No system prompt, small model or security classifier can guarantee perfect instruction following.

## Open-source components

Reuse the existing validation and HTTP stack for this release. No additional agent framework, gRPC service, model download or GPU is required for the bridge's bounded JSON messages.

| Component | Verified code license | Use and deployment boundary |
|---|---|---|
| [Pydantic](https://github.com/pydantic/pydantic) | [MIT](https://github.com/pydantic/pydantic/blob/main/LICENSE) | Already used. `ConfigDict(strict=True, extra="forbid")`, field bounds and model validators enforce packet contracts. These validate structure, not factual correctness. |
| [HTTPX](https://github.com/encode/httpx) | [BSD-3-Clause](https://github.com/encode/httpx/blob/master/LICENSE.md) | Already used for evidence fetching with `AsyncClient`, explicit timeouts, streamed responses and application-controlled redirects. Existing provider adapters handle their API transports; the bridge does not need a second communication service. |
| [llama.cpp](https://github.com/ggml-org/llama.cpp) | [MIT](https://github.com/ggml-org/llama.cpp/blob/master/LICENSE) | Optional; not installed or running in SASI. Local C/C++ inference needs model weights and sufficient memory. CPU inference is supported; a CUDA build requires the appropriate NVIDIA hardware and toolkit ([build guide](https://github.com/ggml-org/llama.cpp/blob/master/docs/build.md)). |
| [LLMLingua-2](https://github.com/microsoft/LLMLingua) | [MIT](https://github.com/microsoft/LLMLingua/blob/main/LICENSE) | Optional; not installed. Its trained compressor adds encoder weights and [PyTorch/Transformers dependencies](https://github.com/microsoft/LLMLingua/blob/main/setup.py). Memory, latency and retained-fact accuracy must be measured before adding it to the current 1 GiB service. |
| [RouteLLM](https://github.com/lm-sys/RouteLLM) | [Apache-2.0](https://github.com/lm-sys/RouteLLM/blob/main/LICENSE) | Optional; not installed. Learned routing needs query-representative calibration; its documented `mf` and `sw_ranking` routers also require an OpenAI embedding API key. SASI retains its existing provider-independent CPU routing and adds no such dependency. |

Licenses above describe the repositories' code; any future downloaded model weights need their own license checked. The deployed bridge also reuses Python's `json`, `asyncio` and `hashlib` for serialization, cancellation and exact artifact identity. A faster serialization or transport implementation does not by itself reduce hosted-model token charges: savings come from fewer calls, smaller relevant inputs, bounded internal outputs and provider-supported caching. C++/CUDA acceleration applies to inference that SASI actually hosts, not to the internals of remote Claude or Gemini services.

## C++, CUDA and small models

[llama.cpp](https://github.com/ggml-org/llama.cpp) provides C/C++ inference with CPU support and optional CUDA for locally hosted models. It can be a future SLM runtime. It cannot change how many tokens a remote Claude/Gemini endpoint bills for a request.

The deployed bridge uses CPU selection, validation and caching; it is **not a running SLM**. This keeps the existing one-CPU, 1 GiB Cloud Run footprint and avoids adding an unmeasured GPU bill. A future SLM should propose passage IDs or routing choices only. The server must validate those choices, preserve protected instructions, enforce provider policy and fall back when the SLM fails. Do not enable a learned compressor until held-out tests demonstrate that it retains required facts and reduces total cost after including its own inference.

## What to measure

Compare a strong single-model baseline, the old pipeline and the bridge on identical requests, source snapshots and provider versions. Use independent answer oracles where possible and blind human review for research. Record actual input/output/reasoning/cache tokens, total calls, latency, missed facts, invalid packets, unresolved checks and provider charges. Peer agreement and a high self-score are not independent accuracy measurements.

Offline regression tests cover protocol validation, injection-shaped data, full-answer preservation, failure handling, cancellation, budgets and restart behavior. Synthetic call/token reductions establish workflow efficiency on those fixtures only. They do not establish a frontier benchmark score or guarantee equal accuracy on every question.
