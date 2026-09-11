# P3.2 隔离执行与真实受控交付 · 记录

## 0. handoff（每次提交时更新）

- 2026-09-12：
  - 已完成：
    - 代码地图，含 seatbelt 探测与 pytest spike（`code-map.md`）；
    - 计划第 1 版，已送独立评审，结论 READY_WITH_CHANGES（2 P0 / 5 P1 / 6 P2）；
    - 全部意见已接受，计划与验收升到第 2 版（§1）。
  - 下一步：切片 A（D1 + D2），测试先行。
    - 先写 `test_sandbox.py`：8 项探针，外加 setsid 与双重 fork 两个逃逸用例；
    - 用实测确认 `(deny mach-lookup)` 会不会影响解释器。
  - 前置已满足：P3.1 遗留修复已交付（0.9.11），SDK main 为 `f07e66d`。
- 接手须知：
  - 沙箱实验只在 scratchpad 或 `/private/tmp` 里做，做完删除；
  - 同一时间只跑一个 pytest；
  - 真实模型只用 deepseek-flash；
  - 本机没有 docker，seatbelt 可用；
  - `RLIMIT_NPROC` 按整个 uid 计数，**不要用**，设低了会让宿主自己 fork 失败。

## 1. 计划评审处置

第 1 轮评审（`reports/plan-review-round1.md`）结论为 READY_WITH_CHANGES，评审员在本机做了实测。全部接受，已落进第 2 版。

| 编号 | 问题 | 处置 | 落点 |
|---|---|---|---|
| P0-1 | 工作区里的软链会被宿主 `copytree` 解引用，密钥因此外泄 | 接受。复制时用 `copytree(symlinks=True)`，复制后扫到软链就拒绝（`workspace_symlink`）；`snapshot` 也拒绝软链；所有读取先 `lstat`，再用 `O_NOFOLLOW` 打开 | D3；P32-5a |
| P0-2 | `killpg` 回收不了 setsid 出去的孙进程 | 接受。执行时每 100 ms 轮询，记下"曾见后代集合"；结束时逐个 SIGKILL，再加 `lsof +D <workspace>` 扫描，反复扫到没有残留为止；CPU 上限作兜底。`tree_killed` 以实测为准，还有残留就判 ERROR；剩余风险如实写明。验收补上 setsid 与双重 fork 两个用例 | D1；P32-3 |
| P1-1 | 没有独立的内容寻址库，清理工作区会把产物一起删掉 | 接受。新增内容寻址产物库，登记时写入，`storage_uri` 指向它；清理只删工作区；v7 迁移时补写产物库 | D3、D4；P32-5b |
| P1-2 | 规则缺少 signal、mach-lookup、process-info 的限制 | 接受。补上 `(deny signal)(allow signal (target same-sandbox))`、`(deny mach-lookup)`、`(deny process-info* (target others))`；探针加"给宿主进程发信号应被拒" | D1；P32-1 |
| P1-3 | `RLIMIT_NPROC` 会误伤宿主，而且没有内存上限 | 接受。不用 `RLIMIT_NPROC`；进程数和 RSS 在执行期间采样，超了就回收，如实写成软限制 | §2、D1；P32-4 |
| P1-4 | 读取用 HOME 黑名单不够 | 接受。改为"全部拒绝 + 白名单"（只拦 file-read-data）；探针加"读 `/private/var/folders` 下别人的目录应被拒" | D1；P32-1 |
| P1-5 | R13 的回归测试放错了地方 | 接受。Host 不走这段召回；只改 `_RecallAdapter`，回归测试放在 SDK；删掉原生验收里的主对话召回场景 | D5；P32-5 |
| P2-1 | 执行后改内容与 `business_action_id` 冲突 | 接受。执行前改内容，生成新版本；执行后改内容，必须走补偿 | D6；P32-8 |
| P2-2 | 默认 `best_effort` 会让 step07 已有的测试回归 | 接受。TestConfigService 与发布连接器同步标为 authoritative | D7；P32-10 |
| P2-3 | L3 双人审批只能在 SDK 层测 | 接受，并写明测试层级 | D8；P32-11 |
| P2-4 | 探针目标与读白名单必须互斥 | 接受。两者同源计算，并用断言保证互斥 | D1；P32-1 |
| P2-5 | `create()` 的静默复用与登记必须在同一处改 | 接受 | D4 |
| P2-6 | 切片偏大 | 接受。把 P0 与 P1 列为切片 A、C 的显式验收项；C 提前到第 2 个做，因为 SDK 默认部署下软链外泄的路径现在就存在 | §4 |
| 诚实性 | 同一 uid 的残余风险，以及软限制，要写明 | 接受 | §2 |

## 2. 实现与测试

（待填）

## 3. 回归与 wheel

（待填）

## 4. 代码评审处置

（待填）

## 5. 遗留

（待填）
