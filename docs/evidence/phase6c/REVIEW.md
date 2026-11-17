# Codex Phase 6C review — 2026-11-17

Reviewed all modified source, new scenario modules, tests, docs and evidence. Original world/network unchanged. Phase 6B commit 0b48c59 is already on origin/roadsense-v2-edge-ai.

- Reproduced the reported baseline: 468 Python tests pass with loopback permission. Added a recorder regression test (39 Phase 6C cases).
- 65 JavaScript and 60 archived Arduino tests pass.
- 1,501 generated right-hand states match HEAD byte-for-byte after removing wall clock and the new road.traffic metadata: SHA256 11471d49017b568390c9b1acaaf62366bd4cb5e0aaa9fa32d80ef71ca40fc25c.
- Verified all 70 pilot checksums against the retained campaign. Fourteen unique IDs in that campaign, 10,524 samples, 10,510 ground-truth frames; pilot results remain historical rather than a new simulation run.
- Fixed a post-pilot source regression: an inline comment swallowed vehicle x/y fields. Raw pilot truth contains x/y. Added an emitted-frame test.
- Traffic changes preserve physical lanes and change only preferred overtaking order in the safety policy. Checks for collision, blind spots and emergency braking remain unchanged.
- Runner overrides edge URL with 127.0.0.1:8011, removes live-publish configuration, isolates SUMO on 8901, restarts only its own server and stores data outside the repository. Original running Webots and port 8000 server were not changed.
- Phase 6B manual dashboard check is documented in docs/validation.md. Vivaldi automation remains unpassed; no browser test was rerun for these non-UI changes.

Accepted limitations: physical replay variability, v1 approximate box collision proxy, lead-slot-presence metric rather than detection accuracy. SUMO truth can differ from Webots physical traffic during smoothed lane changes. Phase 6D will use physical poses, unique execution IDs, strict timestamp joins and future-window censoring. No pilot data is approved as final supervised data. No Pi actions performed.
