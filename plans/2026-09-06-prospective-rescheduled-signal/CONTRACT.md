# H076 source successor: TIME_DUE from actual RESCHEDULED

2026-09-06, base9f875b5. No repository/ancestor AGENTS.md found; ARCHITECTURE/ARCHITECTURE.md read.
Reserved successor H076, version/build unchanged until source review. Frozen H075 branch and artifacts untouched.

Real Host b1f8afef + installedH075/M616 failed after actual public REVISE→rescheduled registration ACK:
ProspectiveSignalIntent rejects time_due RESCHEDULED→TRIGGERED. Original raw at Host source-oracle
.local-test-evidence/2026-09-06/prospective-timer/r2/command.log. Host does not fake pending.

Only expand TIME_DUE transition_from to PENDING or RESCHEDULED, exact destination TRIGGERED.
Keep time trigger, observed_at>=due, no acknowledgement outbox fields, exact authority/ref/hash validation.
No change to EVENT_OCCURRED, recurring schedules, wire/hash domain/version, or database schema.
New single conformance control persists/reopens authority+ref JSON, verifies exact grant and rejects
terminal start states, wrong destination, early clock, timer/outbox confusion and modified committed intent.
Actual Memory apply/reopen single occurrence remains required Host installed combination after source review/build.
No tests run yet; no model/native/new environment/full suite. H076 version availability to verify before artifact.
