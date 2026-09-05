# SDK delivery operation audit source leaf

2026-09-05. Original every SDK-owned observable operation scope; not Host broadcast,
Memory ingestion, network acknowledgements or exactly-once physical delivery.

Existing delivery_outbox remains authority. Three actual terminal producers (root,
continuation and workflow cancellation) record version0, then each expiry/claim/
release/complete CAS records its real version in the same transaction using existing
run_events. No new schema, Run, subscription, authorization scope or artifact.
Legacy head remains readable; missing old versions remain unverified permanently.

DeliveryDispatcher records a durable handoff immediately before invoking the real
sink. That transaction advances the claimed delivery version and writes a linked
attempt fact. This is invocation authority, not proof bytes reached a receiver:
no recorded settlement is unknown, including cancellation or process crash after
sink success. Sink exception releases retry authority but leaves physical outcome
unknown with the fixed delivery_sink_exception code; arbitrary exception text is
not stored/exported. Successful sink return and complete CAS settle the attempt
completed in the same transaction. Repeated exact complete is not a new attempt.
All retries keep the original idempotency key; sink deduplication remains sink-owned.
Custom UoW without this producer cannot obtain physical-coverage certification.

Public audit distinguishes outbox head, every logical version transition, and each
physical handoff attempt. Sink grouping/idempotency identities/payloads are hashes
or opaque references; only recorded timestamps and handoff-to-settlement interval
are exposed. Current source snapshot completeness does not assert physical success.
A version-side handoff fact detects missing whole attempt pairs. Stable pages pin
original run_events cut; later delivery/restart does not mutate captured pages.
Normalizer6 rejects pre-delivery spools instead of silently mixing coverage claims.

Independent P1 found after initial implementation: commit-then-read could substitute
a competing worker's newer version, permitting old sink to settle another attempt.
Claim, handoff and settlement now construct immutable results while their authority
transaction is held. Three decisive commit-window tests fail before this correction
and pass after; late old settlement fails CAS. No lease/TTL relaxation.

Evidence (ignored .local-test-evidence/2026-09-05/run-operation-audit/):
- delivery-red.log: actual success-before-complete crash had no public attempt.
- delivery-owner-red.log: three real public competing CAS cases fail.
- delivery-owner-green.log:11 PASS0.25s.
- delivery-fixed-adjacent.log:95 PASS4.36s, eight directly affected modules, not full suite.
- delivery-old-middle.log: exact installed0.7.2 sink called once, mutable versions4/5
  missing; new events[0,1,2,3,6,7,8] retain delivery_version_history_unverified.
  Script scripts/acceptance/run_delivery_audit_old_runtime.py removes PYTHONPATH
  and verifies installed0.7.2 site-packages. No paid Provider/native.

Fixed-source independent review is required. Remaining canonical source inventory
and complete producer review still block whole source closure and successor wheel.

Fixedeea3c19 independently scoped ACCEPT; original cross-worker P1 closed by
Dirac's fixed-source probe and independent two-connection after-commit oracle.
Follow-up P2: delivery parent refs use the exact delivery-head namespace/identity;
existing runtime parent normalization is unchanged. Public snapshot and all pages
join parent refs to captured heads across restart. Counterexample red, delivery11
PASS (delivery-parent-red.log / delivery-parent-green.log).
