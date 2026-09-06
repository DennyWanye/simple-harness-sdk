# H076 single candidate and actual Memory consumer

2026-09-06。source06f19ec独审限定ACCEPT，新持久authority/ref协议控制1PASS0.09s，PG67201exit0remaining[]。
版本候选源a252109fe4ca22aa37e696eb5c7a04147b4713a9。首次offline setup因hatchling缓存缺失失败，无wheel；独立小build-tools target补hatchling1.32.0后从fixed git archive成功构建一次。
未双build，不声称reproducible双构建。没有新大env，uv install --offline --no-deps --target，不改H075。

Wheel: `/Users/denny/projects/simple-harness-sdk-short-context-revision/.local-test-evidence/2026-09-06/prospective-rescheduled/artifact-076/build/simple_harness_sdk-0.7.6-py3-none-any.whl`
SHA256: `9db87ee970cf86595e5c914bd311df100e7ca9dfd4006015c774b70f163a5d75`
Manifest: `/Users/denny/projects/simple-harness-sdk-short-context-revision/.local-test-evidence/2026-09-06/prospective-rescheduled/artifact-076/manifest.json`
Installed target: `/Users/denny/projects/simple-harness-sdk-short-context-revision/.local-test-evidence/2026-09-06/prospective-rescheduled/artifact-076/installed-target`

实际Host d3f9720a + installed H076/M616：pending/rescheduled新两例2PASS1.71s；真实reg→dueapply后lostACK→过期reopen同ref→唯一inbox。Host原int/float与assert_claim遗漏失败保留并定向修复，不归SDK绿掩盖。
PG67788exit0remaining[]peak182320KiB；buildPG67631exit0remaining[]peak61776KiB。槽释放；未跑旧suite、模型/native/完整scheduler。
Host raw: /Users/denny/projects/simple_harness-typed-recall-source-oracle/.local-test-evidence/2026-09-06/prospective-timer/r4/。
SDK raw: 本树.local-test-evidence/2026-09-06/prospective-rescheduled/{source-r2,build-r1,build-r2,artifact-076}/。
最终制品/组合限定独审待回，不声称已release。无push/tag。
