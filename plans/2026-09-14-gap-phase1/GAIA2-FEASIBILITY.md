# N6 Gaia2 / ARE feasibility and current calibration

## Current state — 2026-09-14 23:37 CST

The named SDK adapter now uses the official `ScenarioRunner`/`MultiScenarioRunner` custom builder injection points. No upstream fork or replacement of the stock `default` agent was needed. Latest parent verification: 50 PASS / 16.23 seconds against pinned ARE 1.2.0 source `87ebd38f31aafae0f11e14f55617903196236cfb`.

A host-authored dynamic policy Scenario ran through the actual SDK Planner/Worker/Critic and official ARE hard checker using Qwen3.8, window262144, one physical slot: **official hard success, Mission COMPLETED/verification_passed**, 11 calls,58017 known tokens,0 unknown,275.301 seconds. The late policy arrived at simulated1020.01 after initial1000.01; Agent sent the correct eligible A/E JSON at1183.1096. Owned process exited with zero residual members. This is an integration calibration, not a Gaia2 dataset score, 256K near-window result, or comparative mechanism benefit.

The official ARE environment retains its pinned dependencies. The same frozen HF tokenizer runs in a separate Python environment, using SDK request serde over a bounded local pipe. All11 actual wire admission grants match server input usage. Recounting the earlier durable request alone is not the wire oracle: `AgentProviderWire` restores executed tool arguments from the effect ledger on a request copy, and durable canonical key ordering also differs. The original unsuccessful recount is preserved alongside the correct grant audit.

**Still open:** full imported Gaia2 Scenario execution and three phases, independent official judge target/accounting, A2A/noise modes, frozen dataset splits, complete paired model evaluations. The hard-only launcher deliberately rejects unsupported imported/judged/oracle/A2A/non-measured modes instead of reporting fake benchmark completion. Judge availability is pending user information; Flash has0 calls in this two-wave continuation.

Raw evidence remains ignored under Host `.local-test-evidence/2026-09-14/gap-two-wave/`:

|Index|SHA-256|
|---|---|
|are-local-dynamic-v1/result.json|12763e13049fc3d488b25dc4b3efa21652f401284255b496af9d1b87663aef29|
|are-local-dynamic-v1/parent-audit.json|d8d525cc1f6374f01acc1ff6b347f3cab24f69ee2b06750ed99dbf4b7e34c58b|
|are-local-dynamic-v1-process/resource.json|a3f0d10ea7cc24010bc779d4ad298cacd1ae453442760f65aca0e2e2aa899c6b|
|are-official-parent-v3.log|1f456ebe7ecbf84cc0569dfc90a44d6cbc199d36388d13f4b2d90a65d81bc173|

## Historical source-only review (superseded implementation status)


**Verdict: feasible as an adapter project; not ready to run Qwen 256K or Flash 512K.**
The blocker is integration correctness, not a documented model-latency claim: stock ARE exposes only
`default`, and `gaia2-run` both rejects another agent name and hard-codes `default`.  Do not replace
that agent with a fake simple_harness default.  Build a named upstream-compatible adapter/launcher,
freeze it, then perform a no-LLM contract spike before either two-wave model run.  The independent
judge remains an explicit open gate.

## Checked boundary

- SDK checkout: `fb498175e219ff6871f028ebeaa023e4277bbe29` (dirty pre-existing implementation
  changes were not touched).
- ARE upstream inspected at `87ebd38f31aafae0f11e14f55617903196236cfb` (2026-09-14).
- Read the [SDK architecture entry](../../ARCHITECTURE/index.md) and
  [two-wave protocol](TWO-WAVE-EVALUATION.md).  The latter fixes Qwen3.8/256K before an independent
  idle Flash/512K wave, with at most two physical slots; it does not establish an ARE result.
- No model call, credential access, dependency installation, dataset download, UI/process action,
  test, commit, or source modification occurred.  Official source was inspected as streamed remote
  text only; no raw source or test artifact was saved locally.

## Exact ARE extension and lifecycle contract

| Concern | Upstream code fact | Required adaptation |
|---|---|---|
| Custom agent | `RunnableARESimulationAgent` requires synchronous `run_scenario(scenario, notification_system, initial_agent_logs)` and `stop()`; its result is `AgentExecutionResult(output, metadata)`. [interface](https://github.com/facebookresearch/meta-agents-research-environments/blob/87ebd38f31aafae0f11e14f55617903196236cfb/are/simulation/agents/are_simulation_agent.py#L23-L42) | Implement a named `SimpleHarnessAREAgent` at this boundary, not a substitute `BaseAgent`. It must translate only the agent-visible task/tools into one durable SDK Mission/Run and return a traceable `AgentExecutionResult`. |
| CLI registry | `AgentBuilder.list_agents()` returns only `default`; `build()` has only that case. The Click `--agent` choice is built from this list. [builder](https://github.com/facebookresearch/meta-agents-research-environments/blob/87ebd38f31aafae0f11e14f55617903196236cfb/are/simulation/agents/agent_builder.py#L50-L105), [CLI option](https://github.com/facebookresearch/meta-agents-research-environments/blob/87ebd38f31aafae0f11e14f55617903196236cfb/are/simulation/cli/shared_params.py#L75-L90) | Use a pinned upstream fork/patch that adds a distinct registry/config entry, or a direct programmatic launcher with the same runner contract. Record the patch hash in every receipt. |
| Gaia2 restriction | Stock `gaia2-run` asserts that `agent` is absent or `default`; its pipeline passes `agent="default"` into every phase. [CLI](https://github.com/facebookresearch/meta-agents-research-environments/blob/87ebd38f31aafae0f11e14f55617903196236cfb/are/simulation/benchmark/cli.py#L439-L484), [pipeline](https://github.com/facebookresearch/meta-agents-research-environments/blob/87ebd38f31aafae0f11e14f55617903196236cfb/are/simulation/benchmark/gaia2_submission.py#L121-L214) | The fork/direct launcher must retain all three official phases, configurations, noise/A2A parameters, and three runs while selecting the named adapter. This is a protocol-preservation task, not an agent rename. |
| Async and deadline | ARE's scenario loop is explicitly synchronous, including `time.sleep(1)` while idle. [loop](https://github.com/facebookresearch/meta-agents-research-environments/blob/87ebd38f31aafae0f11e14f55617903196236cfb/are/simulation/agents/default_agent/are_simulation_main.py#L230-L329) SDK evaluation uses `asyncio.timeout`, persists admission before effects, and preserves `deadline`/`interrupted` terminal states. [runner](../../src/agent_orchestrator/evaluation/experiment.py) | Run the blocking ARE loop in a bounded worker. On SDK deadline/cancellation, signal `agent.stop()`, wait for worker/process termination and collect final known usage before any judge/finalize. If it does not stop, persist `interrupted`/unknown usage and do not retry or score it as success. |
| Cancellation | ARE `stop()` sets a thread event, checked before/after steps; it cannot pre-empt a currently blocking LLM/tool call. [BaseAgent](https://github.com/facebookresearch/meta-agents-research-environments/blob/87ebd38f31aafae0f11e14f55617903196236cfb/are/simulation/agents/default_agent/base_agent.py#L770-L854) SDK providers take an async cooperative `CancelToken`. [port](../../src/simple_harness/providers/base.py) | Bridge `CancelToken` to `stop()` and separately bound every provider/tool call. A timeout is accepted only after physical worker exit and metered terminal receipt; no documentation establishes Qwen or Flash latency acceptance. |
| Simulated time and notifications | ARE uses `TimeManager.time()` plus pause/resume offsets, and queues timestamped `USER_MESSAGE`, `ENVIRONMENT_NOTIFICATION`, or `ENVIRONMENT_STOP`. [time](https://github.com/facebookresearch/meta-agents-research-environments/blob/87ebd38f31aafae0f11e14f55617903196236cfb/are/simulation/time_manager.py#L11-L120), [notifications](https://github.com/facebookresearch/meta-agents-research-environments/blob/87ebd38f31aafae0f11e14f55617903196236cfb/are/simulation/notification_system.py#L24-L83), [consumption](https://github.com/facebookresearch/meta-agents-research-environments/blob/87ebd38f31aafae0f11e14f55617903196236cfb/are/simulation/agents/default_agent/are_simulation_main.py#L331-L360) | Preserve ARE time as scenario state; map notification events through its queue at their scenario timestamp. Keep SDK wall-clock deadline/accounting separate. Do not turn late environment messages into direct SDK task injection. |

## Judge and hidden truth

ARE documents `run` separately from offline `judge`; the judge validates traces against ground truth.
[Benchmark guide](https://facebookresearch.github.io/meta-agents-research-environments/user_guide/benchmarking.html)
also states that soft validation uses its own judge model/provider/endpoint, while hard checks use no
LLM.  Source confirms the independent `judge_model`, `judge_provider`, and `judge_endpoint` are
passed into the runner. [configuration](https://github.com/facebookresearch/meta-agents-research-environments/blob/87ebd38f31aafae0f11e14f55617903196236cfb/are/simulation/benchmark/scenario_executor.py#L284-L313)

The adapter must give the agent only the agent-facing scenario/tools/notifications.  Oracle events,
ground truth, judge engine configuration, and final score stay host-side; score only after the agent
has stopped.  This matches the existing SDK AppWorld boundary: the episode is created with
`load_ground_truth=False` and finalized after the arm stops. [current seam](../../src/agent_orchestrator/evaluation/appworld.py)

**Judge gate still open:** choose and freeze a separate judge target, endpoint/provider identity,
credentials path, cost accounting, hard-versus-soft checker policy, trace schema, and an oracle-free
adapter test.  Neither Qwen3.8 nor Flash may silently self-judge.  The official default of falling
back to the agent provider when `--judge_provider` is omitted is therefore unacceptable for the
planned comparison. [CLI behavior](https://github.com/facebookresearch/meta-agents-research-environments/blob/87ebd38f31aafae0f11e14f55617903196236cfb/are/simulation/benchmark/cli.py#L435-L437)

## Python/package and publishing facts

- Main ARE package: Python `>=3.10`; its package entry point is `are-benchmark`; dependencies are
  sourced from the pinned [requirements.txt](https://github.com/facebookresearch/meta-agents-research-environments/blob/87ebd38f31aafae0f11e14f55617903196236cfb/requirements.txt) (notably `datasets==4.0.0`, `litellm==1.71.1`, `pydantic==2.10.6`, `mcp[cli]==1.11.0`). [package metadata](https://github.com/facebookresearch/meta-agents-research-environments/blob/87ebd38f31aafae0f11e14f55617903196236cfb/pyproject.toml#L1-L31)
- The separately committed `gaia2-cli` and `gaia2-runner` packages require Python `>=3.12`; the
  runner additionally depends on `datasets`, `requests`, `tqdm`, dotenv, and `gaia2-cli[judge]`.
  [CLI metadata](https://github.com/facebookresearch/meta-agents-research-environments/blob/87ebd38f31aafae0f11e14f55617903196236cfb/gaia2-cli/cli/pyproject.toml#L1-L34), [runner metadata](https://github.com/facebookresearch/meta-agents-research-environments/blob/87ebd38f31aafae0f11e14f55617903196236cfb/gaia2-cli/runner/pyproject.toml#L1-L28). Decide the exact surface before implementation; do not install either in the shared SDK venv.
- Upload is opt-in upstream: `--hf_upload` defaults to `None`; `gaia2-run` uploads only when it is
  truthy, otherwise it writes a local summary. [option](https://github.com/facebookresearch/meta-agents-research-environments/blob/87ebd38f31aafae0f11e14f55617903196236cfb/are/simulation/benchmark/cli.py#L253-L270), [branch](https://github.com/facebookresearch/meta-agents-research-environments/blob/87ebd38f31aafae0f11e14f55617903196236cfb/are/simulation/benchmark/gaia2_submission.py#L268-L292). The SDK repository's release workflow is manual `workflow_dispatch` with required inputs and a release environment; no automatic local upload configuration was found. [workflow](../../.github/workflows/release.yml)

## Concrete next tasks before wave A

1. Freeze an isolated Python environment and exact upstream/fork lock; perform an import-only,
   no-LLM contract spike. Store raw trace/receipt only under
   `.local-test-evidence/<date>/gaia2-are-contract/`.
2. Implement the named `RunnableARESimulationAgent` bridge and registry/launcher change; prove the
   stock `default` bytes/config path remains untouched and that no oracle/ground-truth field reaches
   the SDK request.
3. Add deterministic tests for notification timestamp ordering, simulated-time offset, stop during
   an in-flight operation, worker join, durable `deadline`/`interrupted`, known-versus-unknown
   usage, and no judge-before-stop.
4. Add a host-only official judge adapter with a separately frozen judge identity. Run hard-check
   fixtures first; leave soft judge unavailable until its target and accounting are explicitly set.
5. Freeze one source/config/environment/adapter/judge fingerprint plus `hf_upload=None`, local
   ignored output, Qwen3.8 256K, and physical slots `<=2`. Only then perform wave A. Flash512 gets
   a new frozen identity after A, never a retry/substitution inside A.

## Review record

| Item | Actual record |
|---|---|
| Tool-observed execution time | 39.3 seconds summed from completed read-only command waits; end-to-end review wall time was not independently clocked, so no invented duration is reported. |
| Model calls / secrets / installs / datasets | 0 / 0 / 0 / 0 |
| Tests or source-level spike | Not run; source inspection only |
| Token usage | Not exposed for this review; none asserted |
