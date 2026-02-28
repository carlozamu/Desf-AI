# GPT-OSS 120B (Agentic Use) — Best Practices Playbook

This document distills practical best practices for using GPT‑OSS 120B in agentic workflows, with an emphasis on rigorous evaluation, reproducibility, and production readiness. It is written to be copied into a repo as a living Markdown playbook.

## 0) What “agentic use” means here

Agentic workflows are multi-step programs where the model plans, calls tools (retrieval, code execution, browsing, DB queries, RPA), validates outputs, and iterates under explicit budgets. For GPT‑OSS 120B, the two recurring failure modes are (1) measurement confounds that make results non-comparable and (2) uncontrolled tool behaviors that hide errors.

The core premise: hold prompts, decoding, schemas, tool interfaces, and resource budgets constant, then compare on quality–latency–cost.

## 1) Architecture-informed best practices

### 1.1 Treat 120B as “capacity with bounded compute”

GPT‑OSS 120B is a sparse Mixture‑of‑Experts (MoE) model. Only a subset of experts is activated per token, which means:

- Parameter count is not the same as active compute.
- Latency and throughput are often dominated by attention kernels, KV cache policy, and batching.
- You must log the actual inference stack (framework, kernels, compiler flags) to make results interpretable.

### 1.2 Pin reasoning effort explicitly

If the model exposes a “reasoning effort” control (low/medium/high), treat it as a primary experimental variable, not a hidden default.

Best practice:

- Choose **medium** as the primary baseline for interactive agentic systems.
- Run ablations on **low** and **high** to map the quality–latency frontier.
- Record any tool-call frequency changes induced by effort level.

### 1.3 Long context is a feature with prerequisites

If you rely on extended context (YaRN / long window):

- Document effective token positions (where the relevant evidence starts and ends).
- Document truncation rules and KV-cache policy.
- Note attention kernel availability and any fallbacks, because these frequently change tail latency.

## 2) Agentic system design best practices

### 2.1 Standardize tool schemas and contracts

Agentic reliability depends more on tool contracts than on prompt cleverness.

Best practices:

- Fix a stable function schema for each tool (arguments, types, required fields).
- Version tool schemas and keep changelogs.
- Enforce deterministic outputs from tools where possible (stable retrieval ordering, stable code execution environment).
- Always capture tool inputs/outputs in logs.

### 2.2 “Evidence-first” output design

For any agentic task that touches facts, documents, or external systems:

- Require the model to return *evidence objects* (snippets, IDs, offsets, citations, query traces) separate from final claims.
- Validate that each claim is supported by evidence.
- If evidence is missing or weak, force a retry step that fetches stronger evidence rather than rephrasing.

### 2.3 Structured outputs: JSON Schema as a product feature

If you expect structured outputs:

- Define JSON Schema (or equivalent) per task family.
- Validate strictly.
- Track **schema-pass rate** as a first-class metric alongside accuracy.

Operational trick: make schemas tolerant to minor formatting variation (e.g., enums, optional fields) but strict on meaning-critical fields.

### 2.4 Budgeting: cap everything that can run away

Agentic loops fail by runaway recursion.

Cap:

- Max tool calls per task.
- Max total tokens (input+output).
- Max wall-clock per task.
- Max retries per failure type.

Also log the reason for termination (success, timeout, budget hit, schema fail, tool error).

### 2.5 Build-in self-checks that are *verifiable*

Avoid “self-evaluation” that isn’t grounded.

Prefer checks that can be validated:

- Schema validation.
- Unit tests for code outputs.
- Retrieval citation checks (precision/recall).
- Deterministic post-processing rules.

## 3) Benchmarking and evaluation: best practices

### 3.1 Set clear evaluation objectives

Your evaluation should measure:

- **Quality** on realistic tasks.
- **Latency** end-to-end (including tools).
- **Cost** per successful outcome.

Also measure *where it’s wrong* via failure taxonomy, because that’s what drives engineering decisions.

### 3.2 Threats to validity (and how to neutralize them)

Common confounds and mitigations:

- **Prompt drift** (different system preambles or schemas): freeze prompts in version control.
- **Decoding variance** (temp/top-p/max tokens not pinned): pin all decoding knobs.
- **Hardware variance** (different accelerators/compiler stacks): either normalize or report separate tables.
- **Data leakage / contamination**: deduplicate where feasible; document sources and benchmark snapshots.
- **Selective reporting / cherry-picking**: log all attempts, disclose retry policy, report failure rates.

The gold standard is a preregistered protocol and a run ledger.

## 4) Benchmark suite design for agentic workflows

### 4.1 Coverage: don’t benchmark only what’s easy to grade

Recommended task families:

- Reasoning: math word problems; hard science QA; multi-step logic.
- Coding: synthesis + repair with pass@k.
- Knowledge & reading: multi-hop QA with explicit citation checks.
- Agentic chains: retrieval → plan → tool execution → verification.

### 4.2 Dataset hygiene

- Freeze splits.
- Document sources.
- Keep a changelog (what changed, when, why).
- Track benchmark version IDs in results.

### 4.3 Scoring: include structured compliance

Use the right metric per task:

- Exact match / F1 for short answers.
- Rubric scoring for longform explanations.
- pass@k for coding.
- Citation precision/recall for retrieval.
- JSON schema-pass for structured agent outputs.

If you can’t grade reliably, you can’t optimize reliably.

## 5) Reproducible inference configuration

This is the minimum set of knobs that must be fixed and reported.

### 5.1 Decoding defaults (pin these)

Baseline defaults for reasoning and agentic tasks:

- Temperature: **0.2**
- top‑p: **0.9**
- Max output tokens: set per task and report truncations
- Stop sequences: fixed per task family
- System prompt: fixed per task family

If you deviate (creative writing, brainstorming), label those runs separately.

### 5.2 Reasoning effort

- Primary: **medium**
- Ablations: low / high
- Report quality–latency–cost trade-offs, not just accuracy.

### 5.3 Context window handling

- Report window size and effective token positions.
- Report truncation rules.
- Report kernel/attention implementation.

### 5.4 Tool use protocol

- Standardize tool schemas.
- Count tool calls.
- Measure tool overhead (latency and failures).
- Define failure handling and retry policy up front.

## 6) Metrics that matter for agentic systems

### 6.1 Quality metrics

- EM/F1 for short answers.
- Rubric score for explanations.
- pass@k for code.
- Citation precision/recall for retrieval.
- Schema-pass rate for structured outputs.

Always report uncertainty:

- Bootstrap 95% confidence intervals.
- Per-category breakdowns.
- A failure taxonomy (“wrong because…”).

### 6.2 Latency and speed metrics

Measure both model and pipeline:

- TTFT (time-to-first-token).
- tokens/sec.
- End-to-end task time including tools.

Report distributional stats:

- p50/p95/p99.
- Tail events (near-timeout) explicitly.

### 6.3 Cost metrics

For API usage:

- Effective cost per 1k tokens (input + output).
- Cost per successful task.

For self-hosting:

- Hardware amortization assumptions.
- Power costs.
- Utilization and concurrency assumptions.

A useful derived metric: **cost per correct answer**.

## 7) Minimal-friction experimental protocol

A protocol that is strict enough to be credible, but not so heavy it never runs:

1) Freeze benchmark list, prompts, schemas, seeds.

2) Pin environment: framework versions, kernels, tokenizer.

3) Warmup: one dry run per task to compile/prime (excluded from results).

4) Primary runs: 3 seeds × medium reasoning; temperature 0.2.

5) Ablations: low/high reasoning; shorter/longer context; alternate sampling.

6) Validation: automatic graders + human spot checks on borderline cases.

7) Accounting: log tokens, TTFT, decode rate, tool calls, errors, retries.

8) Analysis: means, CIs, failure taxonomy, cost per correct.

9) Report: publish “what changed since last run.”

## 8) Reporting standards (what to publish)

If it’s not published, it didn’t happen.

Minimum reporting template:

- Model: weights hash/commit, tokenizer version, reasoning level.
- Decoding: temperature/top‑p/max tokens/stop sequences.
- Context: window size, truncation policy, retrieval depth.
- Hardware or providers: accelerator type, memory, compiler; API provider/region.
- Quality: per-task scores + CIs; schema-pass; refusal rates.
- Latency: TTFT, tokens/sec, end-to-end; p50/p95/p99.
- Cost: per task and per correct; assumptions.
- Failure modes: common “wrong because” patterns.
- Replicability: seeds, logs, minimal notebook.

## 9) Pitfalls (and practical fixes)

### 9.1 Schema drift

Problem: tiny format shifts cause silent score drops.

Fix:

- Lock schemas.
- Validate strictly.
- Report schema-pass.

### 9.2 Token starvation

Problem: low max output truncates reasoning or tool steps.

Fix:

- Set generous max-new-tokens.
- Log truncation flags.
- Inspect failures; don’t guess.

### 9.3 Cherry-picking

Problem: reporting only “best of n” or second attempts inflates results.

Fix:

- Disclose sampling policy.
- Report all attempts.
- Report success rate and retries.

### 9.4 Prompt leakage

Problem: accidental hints or contaminated context inflate scores.

Fix:

- Blind graders.
- Sanitize prompts.
- Treat retrieval corpora as part of the benchmark definition.

### 9.5 Apples-to-oranges comparisons

Problem: different kernels, batching, or safety filters change results.

Fix:

- Normalize or report separate tables.
- Always include run cards.

## 10) Interpreting results against peers

When comparing against other models/providers:

- Use identical prompts, decoding, and schemas.
- Expect drift across providers (rate limits, cold starts, safety filters).
- Avoid over-generalizing from one suite to “overall intelligence.”

Interpretation best practice: treat benchmarks as directional; require multiple suites and confidence intervals for strong claims.

## 11) Cost and capacity planning for production

### 11.1 Throughput planning

Service capacity is roughly:

- tokens/sec × concurrency → run rate

Log concurrency settings, batch policy, and queueing behavior.

### 11.2 SLO tuning

- Use medium reasoning for interactive latency.
- Escalate to high only when measured accuracy lift is material.
- Consider dynamic escalation: start at medium, escalate only on detected uncertainty.

### 11.3 Budgeting

- Publish p50/p95 price bands per workflow.
- Include tool-call overhead and retries.
- Re-measure after weight updates.

## 12) Safety, refusals, and monitoring

For agentic deployments, safety is measurable.

Track:

- Refusal rate.
- FalseReject (refuse when it should answer).
- Policy adherence on red-team prompts.

In production:

- Add monitoring for tool misuse, prompt injection, and retrieval poisoning.
- Log and review tail failures, not just average scores.

## 13) One-page run card (copy/paste)

Use this for every benchmark run. Commit it with results.

```text
RUN CARD

Model
- Name: GPT-OSS 120B
- Weights hash / commit:
- Tokenizer version:

Inference
- Reasoning effort: low / medium / high
- Temperature:
- top-p:
- Max output tokens:
- Stop sequences:
- System prompt version:

Context
- Window size:
- Truncation policy:
- KV-cache policy:
- Long-context kernel:

Tools (agentic)
- Tool schema version:
- Retrieval depth:
- Max tool calls:
- Retry policy:

Environment
- Provider (API/self-hosted):
- Region (if API):
- Accelerator:
- Framework + version:
- Attention kernel:
- Batch policy / concurrency:

Metrics logged
- Quality metrics:
- Schema-pass rate:
- TTFT p50/p95/p99:
- tokens/sec p50/p95/p99:
- End-to-end latency p50/p95/p99:
- Cost per task + assumptions:

Notes
- Changes since last run:
- Known anomalies:
```

## 14) Minimal evaluation pseudocode (template)

This is intentionally minimal. Treat it as a skeleton for your harness.

```python
def run_eval(model, tasks, reasoning="medium", temp=0.2, top_p=0.9):
    log = []
    for t in tasks:
        prompt = render_prompt(t.system, t.user, t.schema)
        m = model.generate(
            prompt,
            temperature=temp,
            top_p=top_p,
            reasoning=reasoning,
            max_tokens=t.max_out,
            stop=t.stop,
        )
        log.append({
            "task_id": t.id,
            "seed": t.seed,
            "ttft_ms": m.ttft_ms,
            "tok_s": m.tokens_per_sec,
            "schema_ok": validate_schema(m.text, t.schema),
            "score": grade(m.text, t.gold),
            "input_tokens": m.input_tokens,
            "output_tokens": m.output_tokens,
            "tool_calls": m.tool_calls,
            "tool_failures": m.tool_failures,
            "truncated": m.truncated,
        })
    return summarize(log)
```

## 15) Change management

Agentic systems drift fast. Keep a run ledger and treat evaluations like releases.

Best practices:

- Record model checksum, tokenizer build, kernel version, benchmark snapshot.
- Label results by date (e.g., “2026‑02‑28 run, v0.4”).
- Re-run after any weight drop, kernel change, schema change, or retrieval corpus change.

## 16) Quick FAQ (practical clarifications)

Why does TTFT fluctuate across providers?

Cold starts, compilation, queue depth, and provider-specific safety policies. Measure warm and cold runs separately.

Is a larger model always better?

No. In MoE, active parameters and decoding policy often dominate practical performance.

How should I interpret benchmark deltas?

Use confidence intervals and multiple seeds. Small deltas without uncertainty bounds are usually noise.

Does higher reasoning effort help?

Usually, yes, but it may raise latency/cost and tool-call count. Measure, don’t assume.

Can a single “perfect” prompt generalize?

No. Report means with CIs across diverse tasks and seeds.

## 17) Practical checklist (pre-flight)

Before you trust any reported score, verify:

- Prompts and schemas are frozen and versioned.
- Decoding knobs and reasoning effort are pinned.
- Environment and kernels are documented.
- Tool schemas are stable and logged.
- All attempts, failures, retries, truncations are included.
- Metrics include quality, latency, and cost with uncertainty.

## 18) Bottom line

A strong agentic setup with GPT‑OSS 120B is less about “prompting tricks” and more about disciplined control: pinned inference knobs, verifiable tool contracts, strict schemas, full logging, and honest reporting of tail failures. If you do those things, you can make reliable quality–cost decisions and iterate without fooling yourself.

