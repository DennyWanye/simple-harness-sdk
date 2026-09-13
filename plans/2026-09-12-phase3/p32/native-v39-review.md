# Native code, Critic evidence and pending-review cold recovery

Last updated: 2026-09-14 CST. PASS in the stated scope. Source snapshot-v39: Host18ff5d24 / SDK7f926b2, 335 SDK inputs and 218 loaded module origins attested; existing debug carrier, no packaging.

| Case | Result |
|---|---|
| Same original goal | mission-8afbc56a1a36a134: actual UI submit, two Python files, Worker run_tests and isolated verification pytest4PASS. Planner selected code_test+human_review only. One Attempt, 11535tokens, no retry. This alone does not prove Critic ordering. |
| Explicit Critic goal | mission-680c01d138930baa: separate goal explicitly requires format/rule/critic/code/human. Actual Worker run_tests and verification-copy pytest3PASS. Real Critic requests contain the authoritative test_output section with 3 passed, then CriticPASS and human wait. One Attempt, 15397tokens. |
| Files | Four actual files opened in native UI; full hashes wrap. First math285B hash6b43d7507c92d4d60a8967c704115ecb482069743c127476dda6293a9ef9887a, test286B e8b8b58e54f62cc8570bd37e440aef87a7737b21d3f66a0499e7bf571ffbc47a. Second math69B184dc2fe6e1d2f7108d62731e539aeeeac2b165f4ee5e87607076f6b92082513, test174Bf6848b61d749b43e2bd84a1e670aa78e7ea9d1a22d9a89e13fb9da0e763598b7. |
| Sandbox | Native startup8/8actual OS probesPASS; verification receipts kind=seatbelt, isolated=true, exit0, no residual pids. |
| Pending cold | Both approvals remained pending after normal exit/relaunch. All selected durable-table hashes (including tools, approvals, grants and execution records) exactly match, zero new calls. Actual second test file reopened. |
| Approve after cold | UI approved both, both MissionCOMPLETED/verification_passed, both AttemptsCOMPLETED, all filesVERIFIED. Original10Provider records remain10/0rehandoff, budget26932settled/0reserved. No new model calls for approval. |

Real total10calls26932tokens/cache16384; no claimed savings ratio because Planner policies differ between runs. Lifecycle176.367s native +106.073s cold, includes manual waits; both exit0, no remaining group members. Caffeinate retained. An unsent slot-wait draft was replaced by the explicit Critic goal; no native overlapping-slot wait observation is claimed (software and actual kill controls separate). Snapshot-v38 was accidentally prepared before SDKcommit after a parent documentation command failed, never launched; v39 is the verified correct source.

Raw evidence: Host ignored `.local-test-evidence/2026-09-13/p33-g/source-ui-code-v39/`, screenshots01-12, three table captures and case-summary.json SHA256265a23807de2f53641bed62c00372f063b9138a55e11e4902cedab605f5d6b93. Pending-before529e5fdd078ce7b80062701846cddd89ef194883fd3f3e40078e66b3afe51dc7; pending-after8b695c4b6ae281903c88b460f56dfbfbf395b759d5f68d30f802edf842d6fb9e; after-approvals8c6c4dc5a21dacbb7a9bfc4ec2a6495a84cbdd13443715b68e83600d618121b6. Final cumulative checks and P34 strict pair remain OPEN. Packaging/installer checks deferred.
