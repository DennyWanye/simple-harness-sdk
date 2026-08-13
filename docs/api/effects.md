<!-- SPDX-FileCopyrightText: 2026 DennyWanye -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Durable Tool effects

`EffectExecutor` makes the physical Tool boundary explicit:

1. validate the typed Tool call;
2. obtain a host authorization receipt;
3. acquire a monotonic run fence;
4. atomically persist `prepared` and then `handed_off` before entering the
   handler;
5. settle a matching `ToolResult`, or persist `unknown` if execution is
   interrupted after handoff.

The ledger binds stable `run_id`, `call_id`, `effect_id`, Tool name, canonical
argument hash, authorization receipt, handoff receipt, and fence epoch.
Compare-and-swap versions reject stale or duplicate settlements.

On reopen, a `handed_off` or `unknown` effect is observed through the host
`ToolReconciliationPort`. `completed` settles from external evidence;
`confirmed_not_started` resets the same effect for dispatch under the current
fence epoch; `still_unknown` remains non-dispatchable. The executor never
blindly replays an uncertain effect.
