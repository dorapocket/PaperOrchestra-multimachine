# Devil's Advocate Reviewer Protocol

## Role

One of the simulated peer reviewers is designated the Devil's Advocate (DA).
The DA's job is to challenge the paper's core claims from first principles,
not to find polish issues (those are other reviewers' job).

## DA Attack Targets (in priority order)

1. **Causal overclaiming** — Does the paper say "X causes Y" when it only shows
   correlation?
2. **Ablation coverage** — Does every claimed component have an ablation? If not,
   flag missing ablations.
3. **Baseline fairness** — Are baselines run with the same compute budget and
   tuning effort?
4. **Generalization claims** — Does the paper claim broad applicability from narrow
   experiments (e.g., 1 dataset)?
5. **Novelty inflation** — Is the "novel" contribution already present in cited
   works?

## Concession Threshold

The DA must score each rebuttal from other reviewers 1–5 before updating its
position:

- Score 5: rebuttal directly addresses the attack with paper evidence
  → concession allowed
- Score 4: rebuttal provides strong indirect evidence → concession allowed
- Score 3: partial rebuttal → DA holds position, restates attack more specifically
- Score 1–2: weak rebuttal or no response → DA escalates (marks as CRITICAL if
  unaddressed after all reviewers weigh in)

**IRON RULE:** No consecutive concessions. The DA may concede at most once per two
review rounds.

DA CRITICAL findings block the "refinement accepted" decision regardless of overall
rubric scores.

## What DA CRITICAL Means

If the DA issues a CRITICAL finding, `score_delta.py` exit code is overridden to
2 (REVERT). The revision must specifically address the CRITICAL finding before
continuing.

Log in worklog.json: `{da_critical: true, finding: "..."}`

## Deterministic enforcement: `scripts/concession_guard.py`

The concession threshold and the no-consecutive-concessions iron rule are easy
for a simulated reviewer to quietly relax — it caves. To make them
non-negotiable, record the DA's findings and concession decisions in a
**concession log** and run `concession_guard.py` each iteration. The script
re-derives which concessions are valid and whether any CRITICAL is still
standing; the host agent must obey its verdict over the LLM's prose.

Concession log schema (`workspace/refinement/da_concessions.json`):

```json
{
  "rounds": [
    {
      "round": 1,
      "findings": [
        {
          "id": "F1",
          "severity": "critical",
          "attack": "Sec 4 claims X *causes* Y from correlation only.",
          "rebuttal_score": 2,
          "conceded": false,
          "resolved": false
        }
      ]
    }
  ]
}
```

- `rebuttal_score` (1–5) — the DA's score of the author/revision rebuttal,
  using the concession-threshold scale above.
- `conceded` — did the DA drop the attack this round?
- `resolved` — was the underlying issue actually fixed in the revision?

```bash
python skills/content-refinement-agent/scripts/concession_guard.py \
    --log workspace/refinement/da_concessions.json \
    --out workspace/refinement/iter<N>/da_guard.json
```

Verdict → loop action:

| Guard exit | Meaning | Host action |
|---|---|---|
| 0 | CLEAR — no standing critical, no violations | accept may proceed |
| 1 | BLOCK — a critical is still standing | treat the iteration as **REVERT** (force `score_delta.py` outcome to exit 2) and require the next revision to address it |
| 2 | WARN — a concession was rejected (caving or consecutive) but no critical is blocked | the DA must restate the attack; do not let the rejected concession stand |
| 3 | input / schema error | fix the log |

The guard rejects (does not honor) any concession made at `rebuttal_score < 4`
or in a round immediately following another conceding round, and restores the
affected finding to "standing". A standing CRITICAL blocks acceptance
regardless of rubric scores — this is the deterministic backstop behind the
prose rules above.
