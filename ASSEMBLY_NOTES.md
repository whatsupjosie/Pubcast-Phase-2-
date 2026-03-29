# Assembly Notes

Date: 2026-03-29

This folder was assembled as the current non-destructive source-of-truth candidate for PubCast AI inside repo 2.

Source used:
- `Created Files/PubCast_verified_snapshot_2026-03-28_104725_b/program`

Why this source was chosen:
- It contains the full app structure (`main.py`, `modules/`, `static/`, `assets/`, `data/`, `requirements.txt`).
- Its key split-brain files matched the parallel truthful-inspection copy on the critical hashes checked during assembly.
- It includes the dedicated `modules/split_llm.py` routing layer and both Q4/Q6 GGUF files.

Non-destructive rule:
- Nothing else in repo 2 was deleted or replaced to make this folder.
- Existing archives, snapshots, and created-file bundles were left in place.

Next intended step:
- Use this folder as the candidate publishable program root while we decide what supporting archives and tools belong in a later `work_supplies` area.
