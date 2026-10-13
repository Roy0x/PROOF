# Recorded live Nemotron v0.7 verification

This note summarizes the **already completed**, explicitly opted-in live v0.7 run. It is drawn from the two persisted Proof Packs for that run and the existing project run record. The raw packs, credentials, local artifact paths, prompts, and provider responses are not published here. No new live inference was performed for this note.

The integration used `nvidia/Nemotron-3_5-Lightning` through the Nebius Token Factory API. One bounded model client served four roles: the coding worker chose a constrained source patch; the compiler proposed Outcome Contract conditions; the planner proposed checks for five frozen conditions; and the repair role selected the allowlisted `auth_sessions_v1` migration from failed evidence. Trusted code validated model proposals and executed the fixed repair. Deterministic verifiers, rather than model output, determined both verdicts.

| Recorded field | First verification | Reverification |
| --- | --- | --- |
| Model execution | `live_nebius` | `live_nebius` |
| PROOF verdict | `FAILED` | `VERIFIED` |
| Production `auth_sessions` schema observed | `false` | `true` |
| Completed provider requests at pack creation | 7 | 8 |
| Contract ID | `contract_cc8f980b8e17436fa71f5b6352d76a27` | Same |
| Contract version | `1` | `1` |
| Frozen plan fingerprint | `3f1bc001fdd259dd0a138abe2b95871130e3e9fb773afaf3116233973bfcd624` | Same |

The second pack records the `auth_sessions_v1` repair decision and an `applied` repair outcome. Its usage record reports **8 completed live inference requests**, 5,814 input tokens, 555 output tokens, and 0 reasoning tokens. The first FAILED pack remained separate while the second run collected fresh evidence against the same contract and plan.

This is one controlled local login-app demonstration, not a claim of general reliability, production deployment, or zero cost for future live calls. The default v0.8 browser workspace uses the offline model fixture and makes zero Nebius requests.
