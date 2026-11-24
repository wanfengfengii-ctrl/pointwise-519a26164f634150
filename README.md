# 涂层试片复检工作台

涂层试验室从工位收到具名测值，白班计量员登记原测和勘误，分析员每案派两片，现场班组凭本地首执领取，交班员签发逐片出处和精确均值。

## 运行

需要 Python 3.11，使用标准库。完整回归由项目的标准检查入口运行。演示在临时目录中建立两套独立的分析库和现场库，读取 `samples/root.json` 和 `samples/correction.json`，按实际投递和领取结果签发并重开读取。

```text
python3 -B check_project.py
python3 -B -m review.handoff.workbench demo
```

## 一个班次

1. 用 `init` 补建两库表结构并装入样例根修订。重复初始化保留用户修订、批次当前指针、实收记录及定稿。
2. 用 `record` 提交一份 JSON 测值。格式为 `{"batch_id":"lot","parent_id":null,"readings":[["A","9"],["B","8"],["C","7"]]}`。勘误将 `parent_id` 设为本批当前修订号。`revision` 按固定修订号读回，历史修订保持可查。
3. 用 `open` 按指定修订开两片案。先按微米值降序，再按试片号升序。`deliver` 按分配顺序将当前修订的派发绑定写入现场库，返回消息到站或已知的现场结果。
4. 班组用 `receive` 按收到的派发号领取。现场只需打开自己的库。`sync` 将现场的实际首执核回分析端，`retry` 也可用原派发号续送并尝试领取。
5. 需要勘误时，先登记新版测值，再用 `correct` 提交案号、请求号和目标修订号。目标须是案当前生效修订的同批后继，可以跨过中间修订。
6. `correct` 返回 `effective` 后，仍需实领的名额按新版选片，排除已领试片身份。已经领取的旧派发可按原号重领；尚未领取而被封止的旧派发返回 `refused`，理由为 `superseded_dispatch`。候选耗尽时保持空位。缺少现场处置时返回 `pending`，可用原参数续办，同案另一勘误须等本次办结。
7. `finalize` 接收两份已核回分析库的首执号，核对本案两片的唯一性和实际实收序，签发原读值的精确均值。单项、重复或错案证据返回 `null`，之后可补齐再签。
8. `read` 按案号读回原定稿。报告缺失时为 `null`。合法重复签发返回原定稿。

样例根修订的 A、B、C 分别为 9、8、7 μm，勘误版分别为 0、7、10 μm。A 已领时，若勘误先于 B 首领，则改领 C 并签 19/2 μm。若 B 已先提交实收，则保留 A、B 并签 17/2 μm。报告按现场实收序保留每片的试片号、原修订、原值、派发号和首执号。

## 命令和 Python 接口

`python3 -B -m review.handoff.workbench --help` 列出命令。全局 `--analysis`、`--field` 在子命令之前指定库路径。`init` 建库，其余命令打开已有库。`--sample` 可在 `init` 时指定样例 JSON，省略时使用项目根修订样例。仅打开分析库调用 `correct` 可登记待核请求，恢复时再指定现场库。

```text
python3 -B -m review.handoff.workbench --analysis analysis.sqlite3 --field field.sqlite3 init
python3 -B -m review.handoff.workbench --analysis analysis.sqlite3 open case-1 1
python3 -B -m review.handoff.workbench --analysis analysis.sqlite3 --field field.sqlite3 deliver case-1
python3 -B -m review.handoff.workbench --field field.sqlite3 receive case-1/1
python3 -B -m review.handoff.workbench --analysis analysis.sqlite3 --field field.sqlite3 sync case-1/1
python3 -B -m review.handoff.workbench --analysis analysis.sqlite3 --field field.sqlite3 retry case-1/2
python3 -B -m review.handoff.workbench --analysis analysis.sqlite3 finalize case-1 receipt-1 receipt-2
python3 -B -m review.handoff.workbench --analysis analysis.sqlite3 read case-1
```

以上命令使用两个新库，因而首执按 `receipt-1`、`receipt-2` 分配。已有库应使用实际返回的首执号。JSON 输出用字符串表示精确分数。读值可用整数或分数字符串，Python 接口还接受 `Fraction`。缺值、布尔值、浮点数、零分母、重复试片、非法父修订和同号改参均拒绝。试片号为非空字符串，排序使用 Python 字符串顺序。CLI 对输入错误给出非零退出码，业务待核、现场终局拒绝及未结案有各自的返回值。

`review.storage.connect` 打开连接，`review.handoff.workbench.initialize` 建表及可选样例。六个业务入口分别为 `MeasurementCatalog.record(batch_id, parent_id, readings)`、`review.pick.case.open(db, case_id, revision_id)`、`review.pick.replan.correct(db, channel, case_id, request_id, revision_id)`、`review.field.acceptance.receive(field_db, envelope)`、`review.close.publish.finalize(db, case_id, evidence)` 及 `review.close.history.read(db, case_id)`。`finalize` 可提交首执号或完整 `Receipt`。`DeliveryChannel.send` 持久交付绑定，`retry_dispatch` 续送原派发，`sync_dispatch` 读取现场结果并核对入账。独立连接分别提交，现场首领、原终局重领和资格封止均使用现场库的持久记录。待核期间案的生效修订保持原值，现场已经裁决的旧件保留其处置，重办可继续核齐。

## 联合复检派样

同批多案以一个任务号联合派样，先固定整组归属再分批送件。`review.pick.joint.open_joint(analysis, channel, task_id, revision_id, cases)` 受理 `(案号, 候选片号集合)` 的有序序列；每案核定两片、任务内每片仅归一案，按精确厚度降序、片号升序在所有可配满安排中取字典序最前者。返回 `ready`（`assignments` 为按案序排列的 `DispatchAssignment` 元组）、`shortage`（缺口案组 `cases` 及其候选并集 `candidates`，整单不派发）或 `pending`（现场暂不可确认，凭原参数续办）。同号同参续办保持归属与派发号稳定，同号改参抛 `ValueError`；联合案的案号对其他开案保留。

`review.handoff.retry.deliver_joint(analysis, channel, task_id, dispatch_ids)` 只送指定派发，按请求顺序返回 `FieldOutcome` 元组，暂未确认时为 `None`，原号可重发。逐片交接用 `DeliveryChannel.send_joint(task_id, envelope, permit=None)`：现场依据工作台发布的固定归属核对任务、案号、派发号、片号、固定修订和原值；错挂或同号异内容抛 `ValueError` 且不占用真派发号，证据暂缺返回 `None` 待核。分析连接关闭后现场仍可首领已到件，未到件与在途归属均受保护。联合案继续用原 `sync_dispatch`、`finalize`、`read` 逐案核回与结案；`correct` 对联合案抛 `ValueError`，后续测值修订不改变联合安排，普通单案行为不变。`python3 -B -m review.handoff.workbench demo` 末尾以 `samples/joint.json` 演示真实归属、分批领件、错挂拒绝及两案分别结案。
