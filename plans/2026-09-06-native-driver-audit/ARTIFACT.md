# H078 固定候选

2026-09-06。源码 `13abfe8` 及4个真实Runtime新控获Dirac限定ACCEPT；后继版本/快照源码 `f778cba9c5ee599e7ff5ac55796d0f331d215f62`。一次离线构建，无schema变更，无旧制品覆写/发布。

- wheel `simple_harness_sdk-0.7.8-py3-none-any.whl` SHA256 `5aa1112803b5b617142b015f4989c679b455f34444f512a4ede957161b41138e`。
- manifest SHA256 `6a4292634fa0666593d73aba876aff223ef043687c82ec3a864310e3ae8013b6`，execution schema9。
- installed实际普通Runtime＋API精确快照＋077导出保留：3PASS/0.41秒。173个包成员全部与wheel一致；128已加载SDK模块全部来自新target。
- 构建PG99195、installed PG99316均正常退出/remaining=[]，默认共享锁释放。
- Dirac制品及Host `a0a44485` 接入限定ACCEPT：Host实际构造、原精确控制authority拒绝边界、锁/manifest、candidate身份4PASS/1.91秒。新native审计仍待验证，旧unverified区间不追认。

原始索引 `.local-test-evidence/2026-09-06/native-driver-audit/`；命令为现有primary-m0615 Python+Host shared runner默认2048MiB/180秒，子脚本build078.py和installed_consumer.py，不建venv/联网取包。以下原始文件均ignored：

| 文件 | SHA256 |
|---|---|
| build078.py | 19d0d22d2b3635b33b6087790267b3ee733011db3e452d644a5a1f2007be8e18 |
| build-resource/command.log | f0ed899bde0211378b00ecae3ab8530f14efcd6854c76dc191f4829a80c51c87 |
| build-resource/resource.json | 93b461c7e6a717af8570f1b7da08ddf5c6d5ce3ff2301bd2da1fb323a4acf72d |
| installed_consumer.py | d2cf9183af9817753e2509d770bdfb50c425ea67d9f4de856e80b0bb87cbbf3f |
| installed-resource/command.log | 786b493e290582ff456045dc5a4eaa819162ba5164f9a2306d01c86f4d15f3fc |
| installed-resource/resource.json | faf76baafd6aed34eb26aff69b3c94536c58c9cc85f8bb2a1f67b5eb5f872121 |
| installed-resource/identity.json | 7466c37e144b9c51a0fe99ba44ba546443cf5b5be92a3ded40b59fa5597a9731 |
