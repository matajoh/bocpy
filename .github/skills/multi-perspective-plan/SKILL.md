---
name: multi-perspective-plan
description: "Multi-perspective planning with adversarial review loop. Use when: planning complex changes, designing architecture, evaluating implementation strategies, drafting implementation plans, or when /plan is invoked. Spawns three planner subagents, synthesizes their outputs, then iteratively hardens the plan through an adversarial review loop until it passes scrutiny."
argument-hint: "Describe the change or feature to plan"
---

# Multi-Perspective Planning

Generate a robust implementation plan by soliciting three competing viewpoints,
synthesizing them, and then hardening the result through an adversarial review
loop.

## When to Use

- Planning non-trivial code changes that touch multiple subsystems
- Evaluating architecture or design trade-offs
- Any time you want a plan stress-tested before implementation

## Procedure

### 1. Gather Context

Before spawning planners, collect enough context about the target code so each
subagent can work from the same facts. Read the relevant source files and tests.
Summarize the current state in a brief context block that will be included in
every subagent prompt.

### 2. Spawn Three Planner Subagents

Launch three subagents **in parallel**. Each receives the same context block
plus a persona directive. Each must return a concrete, step-by-step
implementation plan (not just commentary).

| # | Persona | Directive |
|---|---------|-----------|
| 1 | **Speed** | Obsessed with performance. Minimize latency and overhead at all costs. Inline aggressively, avoid abstractions, prefer lock-free and wait-free primitives. Tolerate complexity if it buys speed. |
| 2 | **Usability** | Prioritize clean, readable, maintainable code. Favor clear abstractions, good naming, and small functions. Accept modest performance cost for clarity. |
| 3 | **Conservative** | Minimize the changeset. Touch as few lines as possible. Prefer surgical edits over refactors. Reuse existing patterns. Resist new dependencies or abstractions. |

Each subagent prompt must include:

- The shared context block
- The persona directive (from the table above)
- A request for a **numbered step-by-step plan** with rationale per step
- A request for **risks and mitigations** specific to their perspective

### 3. Review the Three Plans

After all three subagents return, review their outputs yourself. Write a brief
analysis noting:

- Points of agreement (high-confidence decisions)
- Points of disagreement (trade-offs to resolve)
- Any gaps none of the planners addressed

### 4. Synthesize

Send all three plans **plus your analysis** to a fourth subagent with the
directive:

> You are a senior engineer synthesizing three competing implementation plans
> into one final plan. Preserve the strongest ideas from each perspective.
> Where planners disagree, make an explicit trade-off decision and justify it.
> The final plan must be a numbered step-by-step implementation sequence with
> clear rationale. Flag any unresolved risks.

The output of this step is the **draft plan**.

### 5. Adversarial Review Loop

Iteratively harden the draft plan by running adversarial reviews until the plan
passes scrutiny. Each iteration proceeds as follows:

#### 5a. Spawn Adversarial Reviewer

Launch a fresh subagent with the directive:

> You are an adversarial reviewer. Assume this plan is wrong. Actively try to
> break the design. Look for race conditions, deadlocks, ABA problems, platform
> bugs, edge cases, missing error handling, reference counting errors, and
> failure modes. Start from skepticism and only endorse what survives scrutiny.
>
> **Plan to review:**
> {include the full draft plan}
>
> **Codebase context:**
> {include the shared context block from step 1}
>
> **Instructions:**
> - For each issue found, report it in this exact format:
>
>   **[SEVERITY] Short title**
>   - **Location:** plan step number
>   - **Problem:** what is wrong and why it matters
>   - **Suggestion:** concrete fix or remediation
>
>   where SEVERITY is one of: critical, high, medium, low.
>
> - If the plan survives your scrutiny, state explicitly: "LGTM — no issues
>   found."
> - Do NOT fabricate issues. Only report genuine problems.
> - Order findings by severity (critical first).

#### 5b. Evaluate Findings

After the adversarial reviewer returns:

- If the reviewer reports **"LGTM"** (no issues found), the plan is final.
  Proceed to step 6.
- If the reviewer reports findings, address them:
  - For **critical** and **high** findings: revise the plan to fix or mitigate
    each issue. Update the draft plan in-place.
  - For **medium** findings: revise if the fix is straightforward; otherwise
    add as a documented risk in the plan.
  - For **low** findings: note and move on.

#### 5c. Check for Stuck State

If after addressing findings you are **unsure how to proceed** — for example,
the adversarial reviewer raises a concern that conflicts with a core
requirement, or two mitigations are mutually exclusive — **stop and ask the
user** for guidance. Present the specific dilemma and the options you see.

#### 5d. Repeat

Go back to step 5a with the revised plan. Use a fresh subagent each time (no
memory of previous passes).

**Bound:** If the loop has run **3 times** without reaching LGTM, present the
current plan to the user with all remaining unresolved findings and ask how to
proceed.

### 6. Present

Present the final plan to the user for approval. Clearly attribute which ideas
came from which perspective where relevant. Note any risks that survived the
adversarial review as known trade-offs.
