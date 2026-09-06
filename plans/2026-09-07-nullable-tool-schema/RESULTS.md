# Nullable 0.7.10 源码结果

2026-09-07。SDK业务源031fdc6（基H079 manifest f841098b11c40192f1ad6b5676148533567ba706）；Host业务2d64e6e5、测试修正fad81ebb（基主2c02be03）。版本0.7.10已固定，M619/S0313不变，未build/安装/改installed。Dirac源码窄审无确定P0/P1，结果交限定终审。

|批次|结果|资源退出|
|---|---|---|
|nullable-sdk-r1|2PASS 0.00s|PG75872 exit0/remaining[]，0.447s/50656KiB|
|nullable-host-r1|1PASS/1FAIL 1.73s|PG75889 exit1/remaining[]，2.353s/176240KiB|
|nullable-host-r2，仅红1|1PASS 1.61s|PG76045 exit0/remaining[]，2.169s/175488KiB|

累计4个唯一新增控制通过。原Host红是exact reuse提交时夹具ledger缺ExecutionEvidenceIngress，原guard真实拒绝context_route_evidence_authority_missing；只给夹具接真实入口，不mock resolve或放宽guard、不造foreground Run。修后仅重红1，SDK两绿及wire绿不重复。所有组cleanup_error=null，共享锁释放，最低磁盘3684MiB。

SDK控制包含nullable两顺序/六非null类型、约束/required/enum/const、冻结JSON值；畸形union/anyOf/旧string-null/保留字段与byte边界拒绝。Host控制包含真实本地HTTP序列化→SDK参数→handler/ledger：省略与null都合法memoryroute但raw hash不同，四种占位字符串及独立非适用hash拒绝。真实公开归档Scope与BindingAuthority/renderer：null新任务使用独立root且不置require_unbound_run；复用缺pin拒绝；真实exact source/hash复用原root且保留unbound门、旧canonical/binding与文件原样。

这是source组合，tool context与空recall是夹具，Scope由authenticated public control建立；没有实际foreground SDK Run，因此不称完整main/权限交互/模型或质量验证。原C01-10/13/20三个真实FAIL保留。主下一步唯一0.7.10 wheel/installed组合再显式C01-20复验，旧证据不覆盖。旧nonstrict测试只更新类型断言与新schema一致，本轮未重跑。

原始证据全部ignored，根目录：/Users/denny/projects/simple_harness-corpus-clock/.local-test-evidence/2026-09-07/context-null-wire/。carrier每批断言simple_harness及schema.__file__来自本SDK src，__version__=0.7.10，source先于旧target；无vendor链接/凭据读写。

实际命令模板（三个已执行批次/模式见上表，分别sdk、host、host-red；绿色不重复）：

```sh
PY=/Users/denny/projects/simple_harness-primary-candidate/.local-test-evidence/2026-09-06/primary-m0615/venv/bin/python
E=/Users/denny/projects/simple_harness-corpus-clock/.local-test-evidence/2026-09-07/context-null-wire
"$PY" /Users/denny/projects/simple_harness-test-resource-cleanup/scripts/run_resource_bounded.py --evidence-dir "$E/<批次>" --rss-mib 2048 --seconds 180 -- "$PY" -B "$E/run-nullable.py" <模式> "$E/<批次>"
```

仅新增/改变成员与必要证据SHA-256（S=本SDK树，H=自有Host树）：

|路径|SHA-256|
|---|---|
|S/src/simple_harness/tools/schema.py|c582e7b9e9da66428580a2c8495e32a0bd39a6cd250fc42a2aae48d0bdfe39fb|
|S/src/simple_harness/version.py|15e594880e630dbcff98dfb70117bed223945e9aca035890cfdb020c2a30f7b6|
|S/tests/unit/tools/test_nullable_schema.py|6a91c58aa2237959fdff7e0664c8991106eadc036d8f6e8779e58a7256f6be59|
|H/backend/deskpet/sdk_adapters/context_route.py|41334c7a0357c54cad3ccac2ef54c619c22ba404fceec4ec29542ef0ad871e79|
|H/backend/tests/sdk_adapters/test_context_route_nullable.py|12aed4eb5ac683ce1f3d5c79eee7566803f0ecc09abd9405ae386b79c7627bb4|
|H/.local-test-evidence/2026-09-07/context-null-wire/run-nullable.py|9e33ff49e98af7f3409d6823d7f476cdc39a30689f9ca5cb89d430462fd0b86d|
|H/.local-test-evidence/2026-09-07/context-null-wire/nullable-sdk-r1/command.log|f9991bfc1c7ebd12bb16ce56e1cf3117618ca286795cd280e4f6ac597c67ff48|
|H/.local-test-evidence/2026-09-07/context-null-wire/nullable-sdk-r1/resource.json|c93988d9635f591160688d90dcead7571dce50f75de4f82bd07a78ffc15f43be|
|H/.local-test-evidence/2026-09-07/context-null-wire/nullable-sdk-r1/origins.json|f59b4015b4f6a627c8d16b6ceadd144a645e3b7a0d95c3fcf5a852bf8d1c2457|
|H/.local-test-evidence/2026-09-07/context-null-wire/nullable-host-r1/command.log|a62782ab89c0d0077ba82c827a8bf6826d6f56b4699ff7c825e0c74d4e37a72f|
|H/.local-test-evidence/2026-09-07/context-null-wire/nullable-host-r1/resource.json|5b7b8c1104ca890dcdd107ba27d313b69fa855354335d302c732d3bbf67b95ea|
|H/.local-test-evidence/2026-09-07/context-null-wire/nullable-host-r1/origins.json|f59b4015b4f6a627c8d16b6ceadd144a645e3b7a0d95c3fcf5a852bf8d1c2457|
|H/.local-test-evidence/2026-09-07/context-null-wire/nullable-host-r2/command.log|fa2024f7b1addf6135eacdcff31a6852f6b6f0275ea8d7ad40bf57f96bb7ea06|
|H/.local-test-evidence/2026-09-07/context-null-wire/nullable-host-r2/resource.json|48726893476e834189889b4865e12c260779047f25fe8adf86552b1e5c99bca0|
|H/.local-test-evidence/2026-09-07/context-null-wire/nullable-host-r2/origins.json|f59b4015b4f6a627c8d16b6ceadd144a645e3b7a0d95c3fcf5a852bf8d1c2457|
