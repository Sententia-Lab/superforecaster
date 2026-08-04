# Superforecasting Methodology

A framework for making calibrated probabilistic predictions, based on Philip E. Tetlock's *Superforecasting*.

---

## A. Question Decomposition

Done *before* any forecasting begins.

### 1. Fermi-ize the question
Break a vague question into tractable sub-questions. Convert "Will X happen by Y?" into a chain of conditional probabilities.

*Example:* "Will Company A acquire Company B by Q4?" becomes:
P(A is looking to acquire) × P(B is a plausible target | A is looking) × P(deal closes in timeframe | interest exists)

### 2. Separate the knowable from the unknowable
Some sub-questions have lookup-able base rates; others require judgment. Tag each sub-question as *researchable* or *judgment-required* so effort goes where it matters.

### 3. Define resolution criteria precisely
Specify the exact observable event that counts as "yes," the resolution source, and the resolution date. Ambiguity here silently corrupts everything downstream.

---

## B. Forming the Estimate

### 4. Outside view first (base rates)
Before considering case-specific details, find the reference class and its base rate. *"How often do startups at this stage get acquired within 12 months?"* This anchors the estimate to reality before the narrative pulls it away.

### 5. Inside view second (case specifics)
Adjust from the base rate using case-specific evidence. The inside view *modifies* the outside view — it does not replace it.

### 6. Regression to the mean
Extreme recent signals usually revert. When something looks far above or below its reference class, weight the reference class more heavily. In short: don't overreact to one data point.

### 7. Dragonfly eye (multiple perspectives)
Consult several reference classes or analytical lenses and aggregate them. When they disagree, that disagreement is itself information about uncertainty.

### 8. Probabilistic thinking with granularity
Don't round to 10% increments. Superforecasters using finer gradations (e.g., 63% vs. 65%) scored measurably better. Use the full 0–100 range.

### 9. Distinguish signal from noise
Ask: *would this evidence change my estimate if I saw its opposite?* If not, it's noise dressed up as signal.

---

## C. Updating Over Time

### 10. Frequent, small updates
Belief revision should be incremental as evidence arrives. Large swings usually mean overconfidence — before, after, or both.

### 11. Bayesian-flavored updating
For new evidence, ask: *how likely would I see this if my hypothesis is true vs. false?* Adjust accordingly. The discipline of asking the question provides most of the value; formal Bayes isn't required.

### 12. Watch for under- and over-reaction
Under-reaction is more common (anchoring on the prior). Dramatic news can trigger over-reaction. Both are failure modes — check for each.

### 13. Post-mortem every resolved forecast
Separate *process* errors from *outcome* noise. A 70% forecast that resolves "no" isn't necessarily wrong — evaluate whether the reasoning was sound.

---

## D. Cognitive Hygiene

### 14. Actively search for disconfirming evidence
Steel-man the opposing view. Ask *"what would change my mind?"* before updating, not after.

### 15. Track and counter known biases
Primary offenders: confirmation bias, availability heuristic, narrative fallacy, scope insensitivity, anchoring.

### 16. Calibration over boldness
A well-calibrated 60% beats a miscalibrated 90% over time. Resist the pull toward confident-sounding round numbers.

An extreme probability is not forbidden — some questions really do resolve at 0.5%. But it has to be argued for: which reference class carries the extreme, why the spread between the classes does not undercut it, and what would have to be true for it to be wrong. If that argument cannot be written down, the number is wrong and should move. See ADR 29.

---

## On confidence

Confidence and probability are different quantities, and conflating them is a live failure mode rather than a hypothetical one.

**Probability** is the forecast. It already carries the uncertainty: 0.60 means this resolves yes about 60 times in 100. There is no separate "how sure am I" to state on top of it, and a forecast-level confidence label is not a second dimension of the answer — it is a restatement of the first, or it is nothing.

**Confidence**, where the term is used at all, belongs to the edge between a *source* and the *claim it supports*: how strongly does this particular piece of evidence back this particular claim? It is a property of the evidence, not of the forecaster's feelings about the number.

    high    directly on point, from something positioned to know
    medium  relevant but indirect — adjacent population, older data, partial coverage
    low     suggestive only: a single report, an analogy, an interested party's claim

A claim is graded by its **strongest** source, not its average one. Citing a solid dataset alongside a weak blog post does not make a claim worse supported than citing the dataset alone; grading by average would teach an agent to hide its weaker sources, which is the opposite of what this is for.

A forecast-level figure may be **derived** from these — weighted across claims by how much each one matters — but it is never asserted. There is deliberately no field in which a model can state its own confidence. A previous version had one, gated a check on it, and the model learned to lower its own confidence label to clear the check. See ADR 29.

---

## Quick Reference Checklist

Before submitting a forecast, verify:

- [ ] Question is decomposed into sub-questions with clear resolution criteria
- [ ] Base rate (outside view) has been established first
- [ ] Inside view adjustments are explicit, not implicit
- [ ] Multiple reference classes have been considered
- [ ] Regression to the mean has been applied where signals are extreme
- [ ] Probability uses fine granularity (not rounded to nearest 10%)
- [ ] Disconfirming evidence has been actively sought
- [ ] Every base rate cites at least one source, graded for how well it supports that claim
- [ ] The aggregate base rate is the weighted average its own reference classes imply
- [ ] A probability outside the calibration band carries a written justification
- [ ] Reasoning trace is recorded for later post-mortem
