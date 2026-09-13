# Phase3 long input context slice

plan-status: finalized (user continued after accepting default256K/selectable512K scope, 2026-09-13)

Original obligations: P34-A06, P35-A04/A07; previous8K-to32K output discussion is independent. User authorizes implementation, real DeepSeek Flash tests and source-native UI; no packaging/release/push. Old tests/materials/contracts and failed real runs remain unchanged. The unrun out32k-v7 experiment patch is archived locally and removed from the working diff because it leaves no Planner/Manager headroom.

## Oracle before implementation

| ID | Input / action | Required result |
|---|---|---|
| LC1 | Fresh official DeepSeek source deployment | Default new Mission uses256*1024 inputtokens;512*1024 selectable; output default8192/ceiling32768 independently; actual profile shown |
| LC2 | Existing32K or pre-context pool, create new long Mission, restart | Old Mission routing, pending request identity, context and source library remain unchanged; new Mission keeps selected capacity even if next creation default changes |
| LC3 | Invalid/unknown capacity or absent required profile; samekey differentcapacity | Reject before any model call; samekey/samecapacity returns originalreceipt; no silent downgrade |
| LC4 | Task with Critic and limited Mission budget | Planner sees capacity-aware minimum; firstCritic full input+output protected; settled/unknown spending retained; unbounded budget and duplicate allocation forbidden |
| LC5 | Genuine256K and512K near-cap inputs through SDK | Pinned counter proves actual input size; beginning/middle/end facts recovered and cross-position condition answered; request and usage recorded without secret; do not substitute file bytecount for tokens |
| LC6 | Required instructions + over-cap history, restart | Current requirements and pending tool protocol remain valid; bounded rotation only when actual budget needs it; frozen request replay causes no new call |
| LC7 | Source native UI create/select/read/cold | Capacity selectable, selectedcapacity displayed for actual Mission, actual deliverable opens after restart with no duplicate call; controlled mechanism and realprovider evidence labelled separately |
| LC8 | Legacy/wheel/unrelatedendpoint | No fabricated official capacity/tokenizer; old receipts/hashes remain compatible; installed wheel capability not claimed |

A successful API response alone does not close LC5; exact context length and correct content are required. Normal short tasks do not fill unused capacity. New long-context experiments may need a larger explicitly declared total budget; both real comparison arms must use the identical contract. Do not silently modify the old2M experiments. Scenario classes: long reference fact retrieval, cross-position constraint synthesis, multi-turn history/recovery; negative: over-cap protected content and unknown profile.

## Work and current state

Parent: Host/UI wiring, SDK integration, sole tests/API/native runner. Sol/high: independent freezing/routing/budget challenge. Terra/medium: disjointP36functionalcontrols. Source baseline SDK753b61a/Host383d0835, previous214PASS plus clean42PASS and nativev29; new continuation baseline will run affected scope before patch.

VERDICT: IN_PROGRESS — implementation and two real synthetic near-cap probes PASS; LC2 pre-context automatic upgrade, LC6 and LC7 remain OPEN. Results (not oracle changes): long-context-results.md.
