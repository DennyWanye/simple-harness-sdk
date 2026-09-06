# 原生驱动审计缺口与后继

2026-09-06。源码13abfe8，4个唯一新场景首批4 PASS / 0.34秒；PG98967正常退出、remaining=[]，峰79088KiB，默认共享锁释放。源码审查、候选制品与Host接线另验，不能标原生覆盖已完成。

真实r12的Host Run `395d23b0-91ac-5e57-bfef-a26f5736aa70` 已由默认后台读取公开审计98/98条，job enumerated且无现有规则finding，但公开metadata明确 `recording_coverage=unverified`、`driver_or_uow_recording_unverified`、历史partial。Host ProductRootDriverRouter包裹实际ReAct，SDK只认精确标准driver，故该缺口是实际接线导致，不能直接改metadata或自报contract消除。

新增不可变公共StartModeDriverRouter持有ordinary与host_control，kernel只对精确SDK selector按已验证持久snapshot.start_mode解析一次；随后检查并调用同一实际driver。policy指纹保持普通driver兼容；Host自定义authority检查仍由原控制driver承担。SDK不递归解包、不信任Host声明、未知mode拒绝。旧协议、schema9与历史数据均不改。

实际SQLite Runtime新控制：标准router普通Run确实调用Provider一次，连续记录已验证；opaque和SDK router子类实际调用同一Provider但继续未验证；三者关闭/公共重开保持同snapshot零重发。真实Host-control Run仅调控制driver一次，原authority完整保留，重复start不重执行，无Provider/effect且coverage仍未验证。

命令：现有primary-m0615 Python执行Host `scripts/run_resource_bounded.py --evidence-dir <根>/r1 -- <同Python> -I -B <根>/run_controls.py <根>/r1`。carrier只加载本SDK src和既有installed依赖；不装新环境、不加载模型、不运行旧suite。原始根 `.local-test-evidence/2026-09-06/native-driver-audit/`：

- `run_controls.py` SHA256 `7ab9f66e12d2842cc4840c21accc5e26aa571c08ccd247293f7c7a7d1bd37c99`
- `r1/command.log` SHA256 `581a45187c8dcdb9efada67726a61c9d65b51cea9c2988a07cdde593a553d8a5`
- `r1/resource.json` SHA256 `8c3402a6ef6962123530d4c3a548cb8c178249f338b24400b1e22cd94a0480b0`
