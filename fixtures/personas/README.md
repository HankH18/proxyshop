# Persona scripts live in the manifest, not here

The `aggressive` and `honest` persona scripts are the `personas` key of
**`fixtures/manifest.json`**, alongside `fixture_intent`. This directory is deliberately
empty of persona data.

Why: T-045's frozen acceptance test reads the persona script out of the *manifest* —
`_load_fixture_manifest()` searches `fixtures/` for a manifest declaring a `personas` block,
then asserts that `build_persona("aggressive").pitch(intent)` emits **exactly** those scripted
claims, no more and no fewer. A second copy of the script in this directory would be a second
source of truth for the same assertion, and the one the test does not read would drift
silently. Ground truth is approved once, in one document, by one human.

Read them with:

```python
from fixtures.manifest import load_manifest

personas = load_manifest()["personas"]
```

`personas.aggressive.scripted_claims` carries `{key, value, claim_type, truthful}` per claim.
Every `claim_type` there is a key of the approved `claim_type_dimensions` table, so each
scripted lie has a typed route into exactly one trust dimension.
