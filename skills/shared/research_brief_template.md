# Research Brief Template

**Inspired by:** AutoSci (arXiv:2605.31468) — accumulating research context across pipeline stages.

`workspace/research_brief.md` is a living document that each pipeline agent
**reads and appends to** as it completes its work. It gives downstream agents
a synthesized "what we've collectively learned so far" — not just raw files.

---

## How it works

Each agent appends a structured section to this file after completing its step.
The file accumulates across the pipeline in this order:

```
Step 1 (Outline)       → appends §1
Step 3 (Lit Review)    → appends §2           (after citation pool is built)
Step 2 (Plotting)      → appends §3           (after figures are rendered)
Step 4 (Section Write) → reads all §1–§3, then appends §4
Step 5 (Refinement)    → reads all §1–§4
```

---

## §1 — Core Claim and Narrative (written by outline-agent)

```markdown
## §1 · Core Claim and Narrative
_Written by: outline-agent, Step 1_

**Core claim:** <one sentence — the central contribution of this paper>

**Narrative tension:** <what gap or failure does this paper resolve — 1-2 sentences>

**Key novelty framing:** <how outline-agent framed the contribution relative to prior work>

**Outline decisions:**
- Plotting plan: <N> figures; driven by <idea.md|experimental_log.md|both>
- Related Work clusters: <cluster names>
- Section structure: <section titles, one per line>

**Potential weaknesses identified at outline stage:**
- <any claim in idea.md that may be hard to support — flagged for lit review attention>
```

---

## §2 — Literature Landscape (written by literature-review-agent)

```markdown
## §2 · Literature Landscape
_Written by: literature-review-agent, Step 3_

**What the literature says about the core claim:**
<2-3 sentences synthesising how existing work relates to our central contribution>

**Strongest prior work (must address in the paper):**
- <bibtex_key>: <why this work is the strongest comparator or predecessor>
- <bibtex_key>: ...

**Gaps confirmed by the literature:**
- <gap 1 — confirmed by absence of certain papers or explicit statements of limitation>
- <gap 2>

**Baseline comparisons — verification status:**
| Baseline | In citation_pool? | Confidence tier |
|---|---|---|
| <name> | yes/no | high/medium/low/conflict |

**Related Work cluster coverage:**
| Cluster | Papers found | Notes |
|---|---|---|
| <cluster name> | N | <any reconciliation needed?> |

**Anything the section-writing agent should know:**
- <important context not captured elsewhere>
```

---

## §3 — Figure Insights (written by plotting-agent)

```markdown
## §3 · Figure Insights
_Written by: plotting-agent, Step 2_

**Figures produced:**
| figure_id | type | key insight the figure communicates |
|---|---|---|
| <id> | plot/diagram | <what the figure shows> |

**Surprising patterns in the data:**
- <anything unexpected that emerged when rendering the plots>

**Section writing implications:**
- <figure_id> should be discussed in §<section> because <reason>
- <any figure that required data cleaning or interpolation — flag as uncertain>
```

---

## §4 — Drafting Decisions (written by section-writing-agent)

```markdown
## §4 · Drafting Decisions
_Written by: section-writing-agent, Step 4_

**Claims that relied on reconciled outline vs. original:**
- <section>: <what changed and why>

**Evidence chain summary:**
| Section | Key claim | Grounded in |
|---|---|---|
| <§X> | <claim> | <exp_log line / figure_id / citation> |

**Sections where evidence was thin:**
- <section>: <what claim was hard to support — flag for refinement attention>
```

---

## Usage by each agent

### outline-agent (Step 1)

After saving `outline.json`, append §1 to `workspace/research_brief.md`.
Create the file if it doesn't exist.

### literature-review-agent (Step 3)

After `citation_pool.json` is built and `intro_relwork.tex` is drafted,
read `workspace/research_brief.md` (§1 is already there) and append §2.

### plotting-agent (Step 2)

After all figures are rendered and `captions.json` is written, append §3.
Note: Steps 2 and 3 run in parallel. Each appends independently; ordering
doesn't matter since they write to different sections.

### section-writing-agent (Step 4)

Before the single multimodal LLM call, read `workspace/research_brief.md`
(§1–§3) and include it in context alongside `outline_reconciled.json`,
`idea.md`, and `experimental_log.md`. After drafting, append §4.

### content-refinement-agent (Step 5)

Read `workspace/research_brief.md` (§1–§4) before each reviewer call.
Pass the "Sections where evidence was thin" list from §4 as additional
context to the Devil's Advocate reviewer — these are the most likely
targets for CRITICAL findings.

---

## File location

`workspace/research_brief.md` — created and maintained within the workspace.
Not an input file; generated entirely by the pipeline.
