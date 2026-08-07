# evals/

Hand-run smoke tests that hit real APIs. Not part of the runtime; not
part of the CI-style test suite. Meant for "before I promote this
phase to live, does it actually behave like I think it does" moments.

## extractor_smoke — memory extractor

Runs a handful of scripted user messages through the real Haiku
extractor and grades what it would write. Costs a few pennies, prints
in seconds. Use this before flipping `EXTRACTOR_MODE=shadow` to `live`
and any time you change the extractor's prompt, imperative regex, or
shape guard.

```bash
cd /root/chad
source .venv/bin/activate
python -m evals.extractor_smoke
```

Green pass counts are what matter, but read the FAIL cases carefully —
sometimes the case description is wrong rather than the extractor.
Adjust CASES in the script when the reasoning changes.

Nothing here writes to memory.md.
