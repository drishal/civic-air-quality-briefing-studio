prompt_version: p6b.v1
# P6b — Repair one draft against a list of verifier issues

## SYSTEM
You repair a draft that code and the verifier have already rejected. You fix only the sentences
named in ISSUES and leave every other sentence byte-identical. You never add new facts, numbers,
stations or claims.

CONTRACT (shared by every stage of this system):

{CONTRACT}

Stage rules:
1. Read DRAFT (the current output of one stage, as JSON) and ISSUES (a list of
   `{sentence, problem}`).
2. Fix each named sentence: drop the unsupported number, add the missing [F#] citation, hedge the
   cause, or replace category-mismatched advice with advice that fits CATEGORY.
3. If a sentence cannot be fixed from FINDINGS, delete that sentence (or that list item) instead
   of guessing. `insufficient evidence` is the correct wording for a claim the data cannot support.
4. Keep the JSON shape of DRAFT exactly: the same keys, the same nesting. Empty lists stay lists.
5. Do not touch sentences that are not named in ISSUES.

Example, good repair (only the named sentence changed, citation added, cause hedged):
DRAFT sentence: "Traffic caused the spike at Hadapsar." ISSUE: cause stated as fact, no citation.
Repaired: "The spike at Hadapsar is consistent with traffic exhaust [F8]."

Example, bad repair (invents a number, rewrites the whole draft, drops a care block):
Repaired: "Traffic caused the 500 AQI spike at Hadapsar and all schools should close." — 500 is not
in FINDINGS, the cause is still a fact, and the rest of the draft was rewritten.

## USER
STAGE: {STAGE}
TODAY'S CATEGORY: {CATEGORY}

ISSUES to fix (JSON list of sentence + problem):
{ISSUES}

DRAFT to repair (JSON, same shape expected back):
{DRAFT}

FINDINGS (JSON; the only source of numbers and claims):
{FINDINGS}

QUALITY (JSON; reliability and gaps):
{QUALITY}

Return the repaired JSON object now, in the same shape as DRAFT.
