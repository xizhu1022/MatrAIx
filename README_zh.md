# MatrAIx Take-home · Task 2：为 sqlglot 构建 RL 训练任务

[English](README.md) | 中文

本仓库是 MatrAIx take-home 第二题的成果：基于开源 SQL 工具 sqlglot（固定在 v30.33.0）构建一个强化学习（RL）训练任务 **`executor-window-functions`**，并用实验证据检验它是否有效、公平、难以作弊、能提供有用的训练信号、有足够难度。

> 出于保密协议，题目原件（题面、Task 1 代码、官方 `selfcheck.py`）不在本仓库中。

---

## 1. 目标

一个 RL 训练任务由四部分组成：

| 组成 | 文件 | 作用 |
|---|---|---|
| 指令 | `instruction.md` | 交给 agent 的任务描述，也是 agent 唯一能看到的内容 |
| 环境 | `environment/Dockerfile` | agent 工作的断网容器，`/app` 下是 sqlglot 源码 |
| 验证器 | `tests/` | 自动判分程序，输出 0–1 的 reward |
| 参考解 | `solution/solve.sh` | 证明任务可解的标准答案 |

评判标准：

- **有效**：参考解得 1.0，什么都不做得 0.0，结果稳定。
- **难**：强 agent 通常失败。
- **公平**：验证器检查的每一项都写在指令里。
- **防作弊**：agent 在容器里是 root，也只有完成工作才能得分。
- **有训练信号**：部分分要反映真实进度。
- **真实**：像工程师会做的工作。

---

## 2. 整体思路

**选题**。我们实测了 sqlglot 自带的 Python SQL 执行引擎约 45 种功能，发现窗口函数完全不支持，`QUALIFY` 还会静默返回错误结果。选择窗口函数有三个原因：

- 它是真实的功能缺口，不是回滚上游修复。
- 需要同时改动规划器、执行器和优化器，涉及大量 SQL 语义。
- 可以用 DuckDB 做**差分测试**：同一条 SQL、同一份数据，分别交给 agent 改过的 sqlglot 和真实数据库 DuckDB 执行，再比较结果。这满足"按行为判分、不比字符串"的要求。

**工作原则**：

- 先写参考解，再写验证器，最后写指令。自己先实现一遍，才知道真正的难点和歧义在哪里。
- 每一条标准都要有可复现的实验证据。所有证据都按官方评测流程产生。

---

## 3. 任务内容（agent 要做什么）

让 `sqlglot.executor.execute()` 支持：

- **排名函数**：`ROW_NUMBER`、`RANK`、`DENSE_RANK`、`NTILE`、`PERCENT_RANK`、`CUME_DIST`。
- **聚合窗口**：`SUM`、`COUNT`、`AVG`、`MIN`、`MAX` 加 `OVER (...)`，包括默认 frame 的同值行（peers）语义。
- **显式 frame**：`ROWS`、`RANGE`（含数值偏移，降序时方向相反）、`GROUPS`，以及 `EXCLUDE CURRENT ROW / GROUP / TIES / NO OTHERS`。
- **导航函数**：`LAG`、`LEAD`、`FIRST_VALUE`、`LAST_VALUE`、`NTH_VALUE`，都支持 `IGNORE NULLS`。
- **组合用法**：`QUALIFY`、命名窗口及继承、`GROUP BY`/`HAVING` 之后使用窗口、JOIN、CTE、`DISTINCT`、`ORDER BY ... LIMIT`。
- **子查询**：窗口函数出现在 `IN` / `NOT IN` / `EXISTS` / `ALL` / 标量子查询中。其中部分情况需要修复优化器 `unnest_subqueries` 的缺陷。
- **规模**：在 50,000 行的表上，每条查询 20 秒内完成。

---

## 4. 仓库结构

```
README.md / README_zh.md            本说明（英文 / 中文）
submission/task2/
├── NOTES.md                        整体思路、尝试与放弃的方案、AI 使用说明
└── executor-window-functions/
    ├── task.toml                   元信息、超时、资源限制
    ├── instruction.md              给 agent 的完整规格
    ├── README.md                   给评审的英文说明（难度、解法、验证、已知漏洞、迭代）
    ├── environment/                python:3.12-slim（按 digest 固定）+ 原样的 sqlglot 源码
    ├── tests/                      验证器
    │   ├── test.sh                 入口：收紧权限，调用 verify.py
    │   ├── verify.py               判分主进程（root）：完整性检查、启动子进程、DuckDB 对照、计分
    │   ├── runner.py               子进程（nobody 身份）：加载 agent 的 sqlglot 并执行查询
    │   ├── querygen.py             固定种子的数据和查询生成器（10 个能力组）
    │   └── coverage.py             评审工具：统计每项要求被测试的次数
    └── solution/                   参考解：修改 planner.py、executor/python.py、optimizer/unnest_subqueries.py
```

---

## 5. Pipeline

### 5.1 一次评测的流程（官方生命周期）

1. 构建环境镜像和验证器镜像。
2. 从环境镜像启动 agent 容器：root 身份，**断网**，2 CPU / 4 GB。agent（或参考解、或什么都不做）在 `/app` 中工作。
3. 停止容器，**只导出 `/app`**，其他一切丢弃。
4. 启动一个全新的验证器容器，导入 `/app`，运行 `test.sh`，读取 `/logs/verifier/reward.txt`。

由此得出两个约束：agent 在 `/app` 以外安装的东西判分时都不存在；`/app` 里的一切都必须当作不可信代码对待。

### 5.2 参考解

- **规划器**：把带窗口函数的查询拆成两层。内层保留 `FROM`、JOIN、`WHERE`、`GROUP BY`、`HAVING`，并算出窗口需要的所有值，例如 `SUM(SUM(x)) OVER ()` 中的 `SUM(x)`。外层在内层结果上计算窗口函数，再处理 `QUALIFY`、`DISTINCT`、`ORDER BY`、`LIMIT`。两层之间插入一个新的 `Window` 执行步骤。不含窗口的查询完全不受影响。
- **执行器**：每个分区只排序一次。每行的 frame 转换成位置区间，`ROWS`/`GROUPS` 是 O(1)，`RANGE` 偏移用二分查找；`EXCLUDE` 把区间切成最多三段。聚合用预计算结构回答：`SUM`/`COUNT`/`AVG` 用前缀和，`MIN`/`MAX` 用稀疏表（支持字符串、跳过 NULL），`IGNORE NULLS` 用非空位置索引。整体 O(n log n)：5 万行上每条查询 0.5–1.7 秒，而朴素实现超过 60 秒。
- **优化器**：`IN` 子查询含窗口函数或 `QUALIFY` 时，先包一层派生表再去重，而不是直接给子查询加 `GROUP BY`。
- 改动共约 640 行。sqlglot 自带的 227 个优化器和执行器测试在修改前后全部通过。

### 5.3 验证器

**生成数据和查询**：

- 5 个小数据集（1–37 行，含 NULL、大量并列值，行顺序打乱），加 1 个 5 万行的大数据集。
- 按 10 个能力组生成 312 条窗口查询，另有 60 条不含窗口的回归查询。随机种子固定，每次运行完全相同。
- **只生成答案唯一的查询**：
  - 所有 `ORDER BY` 都显式写 `NULLS FIRST/LAST`，因为两个引擎的默认值不同；
  - 结果依赖并列行顺序时，追加唯一列 `id` 作为最后的排序键；
  - `RANGE` 偏移只用在不含 NULL 的整数列上。

**10 个相互独立的能力组**：

| 组 | 内容 |
|---|---|
| `types_nulls` | 类型与 NULL：多键排序、字符串降序、NULL 键、`NULLS FIRST/LAST`、字符串 `MIN`/`MAX` |
| `ranking` | 排名函数 |
| `aggregates` | 默认 frame 下的聚合 |
| `rows_frames` | `ROWS` frame |
| `range_frames` | `RANGE` frame（同值组与数值偏移） |
| `groups_exclude` | `GROUPS` frame 与 `EXCLUDE` |
| `navigation` | 导航函数（含 `IGNORE NULLS`） |
| `composition` | `QUALIFY`、命名窗口、`GROUP BY`、JOIN、CTE、`DISTINCT`、`LIMIT` |
| `subqueries` | 窗口函数在子查询中 |
| `scale` | 5 万行，每条限时 20 秒 |

所有窗口都会用到的"横切"问题（数据类型混杂、NULL）集中在 `types_nulls` 组。其他组只用不含 NULL 的整数列排序、只对数值做聚合。这样一个 bug 只影响它真正所属的组。组内模板轮流使用；覆盖率审计确认每项要求至少被测 3 次。

**计分**：

- 一个组内**所有**用例都正确才得分，reward = 完成的组数 / 10。
- 任何一条回归查询出错，reward 直接为 0，防止为了窗口功能而破坏原有功能。
- 我们还试过"组内门槛"（组内通过率 ≥90% 得半分），用 19 份保存的解实测后没有采用：它只改变了 9 份 agent 解中的 1 份（0.80 → 0.85），却让一个完全没实现 `QUALIFY` 的解从 0.7 涨到 0.8，等于奖励了缺失的功能。

**隔离（防作弊）**：

- 判分主进程用独立的隔离解释器运行，**从不 import `/app` 的代码**。
- agent 的代码只在用 `setpriv` 降权为 `nobody` 的子进程中运行：禁止提权，没有 capability，环境变量清空。
- `/tests`、DuckDB 所在目录、`/logs` 仅 root 可访问。
- 运行前检查 `/app/sqlglot` 是真实目录，并清除 `/app` 下的 setuid 位。
- `/app` 追加在模块搜索路径**末尾**，同名草稿文件无法覆盖标准库。
- 在 agent 代码运行之前安装 Python 审计钩子，拒绝导入 `sqlite3`、`ctypes`、`duckdb`，拒绝加载动态库和启动新进程；`/app` 下的编译文件（原生代码）直接拒绝。引擎必须自己实现，不能委托给其他引擎。
- 每条查询的时限由 root 主进程负责：某条卡住或崩溃只影响这一条，主进程会启动新的子进程继续。回归用例先跑。
- 运行前把 `/app` 设为可读，诚实 agent 用严格权限保存的文件也能正常加载。
- 子进程退出后才计算 DuckDB 参考答案；reward 最后由 root 写入。

### 5.4 我们自己的检验流水线

| 步骤 | 做法 | 结果 |
|---|---|---|
| 本地对照 | 参考解与 DuckDB 逐条比较 372 条用例；运行 sqlglot 自带测试 | 全部通过 |
| 官方自检 | `selfcheck.py --repeats 2` | `RESULT: VALID`（参考解 1.0，no-op 0.0） |
| 哨兵解 | 在参考解中各注入一个真实缺陷 | 见第 6 节 |
| 攻击测试 | 10 种作弊方式各写成一个"解"，走完整评测流程 | 全部 0 分，日志中有拦截证据 |
| 覆盖率审计 | 统计每项要求被测试的次数 | 全部 ≥ 3 次 |
| agent 实测 | 只给 agent 看指令，做完后走完整评测流程 | 见第 7 节 |

---

## 6. 各项标准的证据

| 标准 | 证据 |
|---|---|
| 有效 | 官方 selfcheck 重复运行：参考解每次 1.0，no-op 每次 0.0 |
| 公平 | 验证器能生成的每种构造都在指令中有对应条款；测试中发现的问题均已修复（见第 8 节） |
| 防作弊 | 10 种攻击全部 0 分：写 reward 文件或改判分脚本（Permission denied）、偷用 DuckDB（No module named 'duckdb'）、委托给 `sqlite3`（被审计钩子拦截）、把原生扩展复制进 `/app`（被拒绝）、setuid 提权（仍是 uid 65534）、启动钩子（从未执行）、符号链接替换（被拒绝）、伪造输出（不知道答案）、"万能相等"对象（只接受基本类型）、杀判分进程（Operation not permitted）。关掉子进程内的计时器也没用：主进程的逐条时限仍会判超时 |
| 训练信号 | 只含一个缺陷的参考解得分有梯度：忽略显式 frame 0.3；默认 frame 漏掉同值行 0.6；忘了 `QUALIFY` 0.7；忽略 `IGNORE NULLS` 0.8；降序取负 0.9；缺优化器修复 0.9；破坏 LEFT JOIN 0 |
| 难 | 见第 7 节 |

---

## 7. 评测结果

**同一系列三档模型在最终版本上的结果**（都在真实的断网任务容器中、用同一个 agent 框架运行，各 3 次）：

| 模型 | 3 次得分 | 平均 | 满分次数 |
|---|---|---|---|
| GPT-5.6 Luna（小） | 0.3 / 0.1 / 0.1 | 0.17 | 0/3 |
| GPT-5.6 Terra（中） | 0.5 / 0.9 / 0.2 | 0.53 | 0/3 |
| GPT-5.6 Sol（大） | 0.8 / 1.0 / 1.0 | 0.93 | 2/3 |

reward 随模型能力上升；9 次运行的回归测试全部通过，丢掉的分都来自没完成的能力。中档模型的分数分布最宽（0.2–0.9），是 RL 训练信号最强的区间。

**GPT-5.6 Luna：3 次，都没有满分，平均 0.17。**

| 试验 | 得分 | 通过组数 | 未通过的组 |
|---|---|---|---|
| 1 | 0.3 | 3/10 | types_nulls、ranking、rows_frames、navigation、composition、subqueries、scale |
| 2 | 0.1 | 1/10 | 除 aggregates 外全部 |
| 3 | 0.1 | 1/10 | 除 aggregates 外全部 |

**GPT-5.6 Terra：3 次，都没有满分，平均 0.53。**

| 试验 | 得分 | 通过组数 | 未通过的组 |
|---|---|---|---|
| 1 | 0.5 | 5/10 | types_nulls、groups_exclude、composition、subqueries、scale |
| 2 | 0.9 | 9/10 | composition |
| 3 | 0.2 | 2/10 | types_nulls、rows_frames、range_frames、groups_exclude、navigation、composition、subqueries、scale |

**GPT-5.6 Sol：3 次，2 次满分，平均 0.93。**

| 试验 | 得分 | 通过组数 | 未通过的组 |
|---|---|---|---|
| 1 | 0.8 | 8/10 | types_nulls、subqueries |
| 2 | 1.0 | 10/10 | — |
| 3 | 1.0 | 10/10 | — |

---

## 8. 设计演进与 insight

| 版本 | 新增 | 目的 |
|---|---|---|
| v1 | 小表上的窗口函数，`ROWS`/`RANGE` frame | 第一个完整版本 |
| v2 | 5 万行规模组、`GROUPS`、`EXCLUDE`；参考解 O(n log n) | 不仅要求语义正确，还要求高效算法 |
| v3 | 10 个独立能力组；子查询组（参考解修复优化器）；模板轮流抽取 | 一个 bug 只扣一个组；覆盖均匀 |
| v3.1 | 横切问题（类型、NULL、字符串聚合）集中到一个组 | 失败落在根因所属的组 |
| 最终版 | 审计钩子沙箱、主进程逐条计时、`/app` 可读、回归先跑 | 加固防作弊与公平性 |

**Insight**：

1. **部分分应按相互独立的能力来给。** 把横切问题集中到一个组，每个失败的组都对应一个根因，reward 与真实进度成比例（只含一个缺陷的参考解得 0.3–0.9）。
2. **难度是任务与模型共同的属性。** 用一组能力递进的模型去测，才能看出任务在哪个区间提供有效信号：这里中档模型的分数分布最宽。
3. **验证器本身也是需要测试的软件。** 哨兵解、攻击测试、覆盖率审计和独立评审，都在用它测量 agent 之前加固了它。
4. **规格公平且精确时，难度必须来自语义、效率和模块之间的交互**，这正是规模组、frame 组和子查询组的设计目标。

## 9. 范围与后续方向

- 随机种子固定，使 reward 是 `/app` 的确定函数。
- 不在本任务范围内：窗口内的 `FILTER`/`DISTINCT` 聚合等模板之外的功能。
- 面向前沿模型的更难版本：更大的规模组、更多必须修复优化器才能通过的用例，以及上述功能。
