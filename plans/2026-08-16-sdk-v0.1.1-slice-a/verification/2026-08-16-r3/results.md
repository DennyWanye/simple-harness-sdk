# Slice A committed-HEAD results

Tested HEAD: `70a2b9fe20016772e4a34035fb96520f2f17da1e`

| Scenario | UI | Lane / roots | Business result | Evidence | Status |
|---|---:|---|---|---|---|
| SDK-A1 | No | fresh / 2 | public ReAct no-tool and one-tool completed through clean exact wheel | `receipts/sdk-a1.md` | PASS |
| SDK-A2 | No | fresh / 2 | official and host-owned Workflow Runner paths completed; capability six-stage recovery passed | `receipts/sdk-a2.md` | PASS |
| SDK-A3 | No | temporal-fault / 2 | durable authorization resumed after restart; dual receipts fenced physical Tool to at most once | `receipts/sdk-a3.md` | PASS |
| SDK-A4 | No | temporal-fault / 2 | delivery retried with the same key and one Host-visible effect; startup backlog drained | `receipts/sdk-a4.md` | PASS |
| SDK-A5 | No | fresh + temporal-fault / 2 | hard budget and policy drift both failed closed | `receipts/sdk-a5.md` | PASS |
| SDK-A6 | No | fresh / 1 | four conformance suites executed 22 cases and created no SDK Run | `receipts/sdk-a6.md` | PASS |
| SDK-A7 | No | fresh / 1 | immutable local candidate built twice and exact wheel bytes passed clean Python 3.11 CLI + pytest | `receipts/sdk-a7.md` | PASS |

Full regression: `1181 passed, 2 expected skips`; see `receipts/full-regression.md`. Static gates are in `receipts/static-gates.md`.

Out of this slice and therefore still PENDING: product adapter/cutover, database reset, Simple Harness App desktop E2E, remote three-platform dispatch, tag and Release publication.

