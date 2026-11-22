# Task 2 工作说明：为 sqlglot 构建一个 RL 训练任务

本文说明 Task 2 做了什么、为什么这样做、整个流程如何运转，以及每一项评判标准对应的证据。提交物在 `submission/task2/`，其中 `executor-window-functions/README.md` 是给评审看的英文版技术说明，本文是它的中文概要和补充。

---

## 1. 背景与目标

**题目要求**：基于开源项目 sqlglot（固定在 v30.33.0）构建 1–2 个强化学习（RL）训练任务。一个任务由四部分组成：

| 组成 | 文件 | 作用 |
|---|---|---|
| 指令 | `instruction.md` | 交给 agent 的任务描述，也是 agent 唯一能看到的内容 |
| 环境 | `environment/Dockerfile` | agent 工作的容器，`/app` 下是 sqlglot 源码 |
| 验证器（verifier） | `tests/` | 自动判分程序，输出 0–1 的 reward |
| 参考解（oracle） | `solution/solve.sh` | 证明任务可解的标准答案 |

**评判标准**：有效（oracle 得 1.0、什么都不做得 0.0、结果稳定）、难（强 agent 通常失败）、公平（verifier 检查的每一项都写在指令里）、防作弊（agent 在容器里是 root，也只有做完工作才能得分）、有训练信号（部分分要反映真实进度）、真实（像工程师会做的工作）。

**我们的交付**：一个任务 `executor-window-functions`，按"少而精"的原则把它做到每一条标准都有可复现的证据。

---

## 2. 总体思路

### 2.1 选题方法

先系统地探测 sqlglot 自带的 Python SQL 执行引擎（`sqlglot.executor`）能做什么、不能做什么。实测约 45 种 SQL 功能后发现：

- **直接报错**：窗口函数、`ROLLUP`/`CUBE`/`GROUPING SETS`、聚合 `FILTER`、递归 CTE、`LATERAL`/`UNNEST`/`VALUES`、`PIVOT`、若干聚合和标量函数。
- **静默返回错误结果**：`QUALIFY`（过滤条件被忽略）、`ANTI JOIN`（结果与 `SEMI JOIN` 相同）、`DISTINCT ON`（没有去重）。

选择**窗口函数**有三个原因：

1. **是真实的功能缺口**，不是回滚上游修复（题目明确禁止后者）。
2. **工作量和难度来自真实工程**：需要同时改查询规划器（planner）和执行器（executor），涉及大量 SQL 语义细节。
3. **可以按行为判分**：DuckDB 是一个可以直接在 Python 里调用的真实数据库，能执行同样的 SQL。同一条查询、同一份数据分别交给 agent 改过的 sqlglot 和 DuckDB 执行，比较结果。这种做法称为**差分测试**（differential testing），满足题目"verifier 必须检查行为、不能只比字符串"的要求。

### 2.2 工作原则

- **先写 oracle，再写 verifier，最后写 instruction**。自己先实现一遍，才知道真正的难点和歧义在哪里。每个歧义要么写进 instruction，要么让 verifier 不去测它。
- **每一条标准都要有实验证据**，不靠主观判断。所有证据都通过与官方 `selfcheck.py` 相同的评测流程产生。

---

## 3. 任务内容：agent 需要做什么

让 `sqlglot.executor.execute()` 支持 SQL 窗口函数，范围包括：

- **排名函数**：`ROW_NUMBER`、`RANK`、`DENSE_RANK`、`NTILE`、`PERCENT_RANK`、`CUME_DIST`。
- **聚合窗口**：`SUM`、`COUNT`、`AVG`、`MIN`、`MAX` 加 `OVER (...)`，包括默认 frame 的语义（有 `ORDER BY` 时包含所有"同值行"，即 peers）。
- **显式 frame**：`ROWS`、`RANGE`（含按数值偏移，降序时方向相反）、`GROUPS`（按同值组计数），以及 `EXCLUDE CURRENT ROW / GROUP / TIES / NO OTHERS`。
- **导航函数**：`LAG`、`LEAD`、`FIRST_VALUE`、`LAST_VALUE`、`NTH_VALUE`，都支持 `IGNORE NULLS`。
- **组合用法**：`QUALIFY`、命名窗口（`WINDOW w AS ...`，可继承）、在 `GROUP BY`/`HAVING` 之后使用窗口（如 `SUM(SUM(x)) OVER ()`）、JOIN、子查询、CTE、`DISTINCT`、`ORDER BY ... LIMIT`。
- **规模要求**：在 50,000 行的表上，每条查询 20 秒内完成。

---

## 4. 交付物结构

```
submission/task2/
├── NOTES.md                         整体思路、尝试与放弃的方案、AI 使用说明
├── REPORT.md                        （需要你本人撰写）
└── executor-window-functions/
    ├── task.toml                    元信息、超时、资源限制
    ├── instruction.md               给 agent 的完整规格
    ├── README.md                    给评审的说明（难度、解法、验证、已知漏洞）
    ├── environment/
    │   ├── Dockerfile               python:3.12-slim（按 digest 固定）+ sqlglot 可编辑安装
    │   └── sqlglot/                 与题目提供的源码逐字节一致
    ├── tests/
    │   ├── Dockerfile               验证器镜像：DuckDB 2.0.1 装在仅 root 可读的独立虚拟环境
    │   ├── test.sh                  入口：收紧权限，调用 verify.py
    │   ├── verify.py                判分主进程（root）：完整性检查、启动子进程、DuckDB 对照、计分
    │   ├── runner.py                子进程（nobody 身份）：加载 agent 的 sqlglot，执行查询
    │   ├── querygen.py              固定种子的数据和查询生成器
    │   └── coverage.py              评审工具：统计每项要求被测试的次数
    └── solution/
        ├── solve.sh                 把两个修改后的文件复制进 /app
        └── files/                   planner.py、executor/python.py
```

---

## 5. Pipeline

### 5.1 一次评测（trial）的流程

这是题目规定的评测生命周期，`selfcheck.py` 和我们所有的测试都严格按它执行：

1. 分别构建环境镜像和验证器镜像。
2. 从环境镜像启动 agent 容器：root 身份，工作目录 `/app`，**断网**，限 2 CPU / 4 GB。
3. agent（或 oracle、或什么都不做）在容器里工作。
4. 停止容器，**只导出 `/app` 目录**，其他一切丢弃。
5. 启动一个全新的验证器容器，导入 `/app`，运行 `test.sh`。
6. 读取 `/logs/verifier/reward.txt` 作为 reward。

由此得出两个设计约束：agent 在 `/app` 以外装的东西判分时都不存在；`/app` 里的一切都可能被 agent 动过手脚，verifier 要按"不可信代码"对待它。

### 5.2 参考解（oracle）的设计

改动集中在两个文件，共约 630 行：

**规划器（`planner.py`）：把带窗口函数的查询拆成两层。**

- 内层查询保留 `FROM`、JOIN、`WHERE`、`GROUP BY`、`HAVING`，并输出窗口函数需要的所有值。例如 `SUM(SUM(x)) OVER ()` 中的 `SUM(x)` 在内层算好。
- 外层查询在内层结果上计算窗口函数，再处理 `QUALIFY`（转成 `WHERE`）、`DISTINCT`、`ORDER BY`、`LIMIT`。
- 两层之间插入一个新的 `Window` 执行步骤。

这样复用了现有规划器的分组、排序、去重逻辑，不含窗口的查询完全不受影响。命名窗口在拆分前先展开。

**执行器（`executor/python.py`）：高效计算窗口函数。**

- 每个窗口先按 `PARTITION BY` 分区，再按 `ORDER BY` 排序（复用执行器已有的排序键，保证 `NULLS FIRST/LAST` 语义一致），并计算同值组。
- 每一行的 frame 转换成位置区间 `[lo, hi)`：`ROWS` 和 `GROUPS` 是 O(1) 的算术，`RANGE` 偏移用二分查找；`EXCLUDE` 把区间切成最多三段。
- 聚合用预计算结构回答：`SUM`/`COUNT`/`AVG` 用前缀和（整数精确），`MIN`/`MAX` 用稀疏表（支持字符串、跳过 NULL），`IGNORE NULLS` 用非空位置列表加二分查找。
- 整体复杂度 O(n log n)。50,000 行上每条查询 0.5–1.7 秒；最初的朴素实现同样的查询超过 60 秒。

### 5.3 验证器（verifier）的设计

**生成数据和查询**（`querygen.py`）

- 5 个小数据集（1–37 行），包含 NULL、大量并列值，行顺序打乱；另有 1 个 50,000 行的大数据集。
- 按 6 个档位用模板随机组合出 212 条窗口查询，另有 60 条不含窗口的回归查询。随机种子固定，所以每次运行完全相同。
- **只生成答案唯一的查询**，这是公平性的关键：
  - 所有 `ORDER BY` 都显式写 `NULLS FIRST/LAST`，因为 sqlglot 和 DuckDB 的默认值不同；
  - 结果依赖并列行顺序时，追加唯一列 `id` 作为最后的排序键；
  - `RANGE` 偏移只用在不含 NULL 的整数列上；
  - `GROUPS`/`EXCLUDE GROUP/TIES` 只配合与行顺序无关的聚合使用。

**执行与比较**（`verify.py` + `runner.py`）

- agent 的代码只在一个子进程里运行。子进程用 `setpriv` 降权为 `nobody`，禁止提权、去掉所有 capability、清空环境变量。
- 子进程执行完所有查询后，判分主进程才调用 DuckDB 计算参考答案。
- 结果按多重集比较（不计行序），数值按数值比较（浮点相对误差 1e-9），NULL 必须是 `None`。

**计分**

- 6 个等权档位：排名、默认 frame 聚合、显式 frame、导航、组合、规模。
- 一个档位内**所有**用例都正确才得分，reward = 完成的档位数 / 6。
- 回归门槛：任何一条不含窗口的查询出错，reward 直接为 0，防止为了窗口功能而破坏原有功能。

**隔离（防作弊）**

- 判分主进程用独立的解释器、隔离模式运行，**从不 import `/app` 里的任何代码**。
- `/tests`、DuckDB 所在目录、`/logs` 都设为仅 root 可读写。
- 运行前检查 `/app/sqlglot` 是真实目录（不是符号链接），并清除 `/app` 下所有文件的 setuid/setgid 位。
- 子进程的标准输出被重定向，协议输出带随机标识，只接受 JSON 基本类型的结果值。
- reward 文件最后由 root 写入，覆盖任何已有内容。

### 5.4 我们自己的验证流水线

每一步都有通过条件，不通过就不进入下一步：

| 步骤 | 做法 | 结果 |
|---|---|---|
| 本地对照 | oracle 与 DuckDB 逐条比较全部 272 条用例；跑 sqlglot 自带的 60 个 executor 测试 | 全部通过 |
| 官方自检 | `selfcheck.py --repeats 3` | 3 次均为 `RESULT: VALID` |
| 哨兵解 | 在 oracle 里各注入一个真实可能出现的缺陷，看分数是否合理 | 见第 6 节 |
| 攻击测试 | 把 8 种作弊方式各写成一个"解"，走完整评测流程 | 全部 0 分，日志中有拦截证据 |
| 覆盖率审计 | 解析每条生成的查询，统计每项要求被测了多少次 | 发现部分要求测得太少，见第 7 节 |
| agent 实测 | 只给 agent 看 instruction，做完后走完整评测流程打分 | 见第 8 节 |

---

## 6. 各项标准的证据

| 标准 | 证据 |
|---|---|
| **有效** | 官方 selfcheck 3 次重复：oracle 每次 1.0，no-op 每次 0.0。验证器约 25 秒，超时设置为 900 秒 |
| **公平** | verifier 能生成的每种构造都在 instruction 里有对应条款；测试中发现的 3 处问题已修复（见第 7 节） |
| **防作弊** | 8 种攻击全部 0 分：写 reward 文件/改判分脚本（Permission denied）、偷用 DuckDB（No module named 'duckdb'）、setuid 提权（仍是 uid 65534）、启动钩子文件（从未在判分进程中执行）、符号链接替换（完整性检查拒绝）、伪造输出（不知道答案，只会降低自己的分）、"万能相等"对象（只接受基本类型）、杀判分进程/读测试（Operation not permitted） |
| **训练信号** | 只含一个缺陷的解得分有梯度：不支持显式 frame 0.33；默认 frame 漏掉同值行 0.5；第一版朴素 oracle（正确但慢、不支持 GROUPS/EXCLUDE）0.5；忽略 `IGNORE NULLS` 0.67；忘了 `QUALIFY` 0.67；窗口全对但破坏 LEFT JOIN 0 |
| **难** | 见第 8 节：对中等模型已不构成挑战 |

---

## 7. 迭代记录与发现的问题

**v1 → v2：v1 太简单。**
v1 只有小表和 `ROWS`/`RANGE` frame。Claude Sonnet 只用约 4 分钟就拿到 1.0；检查执行记录，确认它没有访问测试或答案。为此 v2 增加了：

- 50,000 行的规模档，每条查询限时 20 秒。逐行重新扫描 frame 的朴素实现要 60 秒以上。
- `GROUPS` frame 和 `EXCLUDE` 子句。
- oracle 同步重写为 O(n log n)。

v1 的满分解在 v2 上只得 0.5。

**测试中发现并修复的公平性问题：**

1. 有一条 `UNION ALL` 回归查询失败，原因是 sqlglot 原有的 bug，与窗口无关，已替换，避免 agent 为原有 bug 负责。
2. 子进程最初把 `/app` 放在模块搜索路径最前面，诚实的 agent 只要在 `/app` 留一个 `random.py` 草稿文件就会全部失败。改为追加到最后后，"oracle 加一堆同名草稿文件"的变体得 1.0。
3. instruction 缺少 `RANGE <start>` 简写和总时限两处说明，已补上。

**覆盖率审计的发现（尚未修复）：**
生成器对模板做的是随机抽取，导致以下要求只被测了 1–2 次：带窗口的 CTE、JOIN、LEFT JOIN、子查询、`GROUP BY ... HAVING`、`DISTINCT`、按窗口表达式排序、`QUALIFY` 引用别名、`RANGE` 简写、`RANGE` 值偏移（升序和降序各 2 次）。改进方法是模板改为轮流抽取，并增加每档用例数。

**新发现的更难方向（尚未纳入任务）：**
窗口函数放进 `IN (子查询)` 时，sqlglot 优化器的 `unnest_subqueries` 规则会改坏查询：

- 子查询带 `QUALIFY` 时报错，因为优化器给子查询加了 `GROUP BY`；
- `x IN (SELECT LAG(y) OVER (...) FROM t)` 会**静默返回重复行**。

我们的 oracle 和 Sonnet 的解都在这里失败。原因是错误发生在优化器阶段，agent 自己写的暴力参考也拿到同一棵被改坏的语法树，所以查不出来。这是 v3 最值得加入的内容。

---

## 8. 难度实测结果

**方法**：agent 只能看到 `instruction.md` 和一份干净的 sqlglot 源码。做完后，把它的 `/app` 放进与正式评测相同的流程打分，并检查执行记录确认它没有访问测试或答案。

| 版本 | 模型 | 运行环境 | 用时 | reward | 说明 |
|---|---|---|---|---|---|
| v1 | Claude Sonnet | 宿主机 | ~4 分钟 | 1.0 | 朴素实现 |
| v1 | 更强的 Claude 模型 | 宿主机 | ~33 分钟 | 1.0 | 额外修复了若干 sqlglot 原有 bug |
| v2 | 上面两个 v1 解重新打分 | — | — | 0.5 / 0.5 | 缺 `GROUPS`/`EXCLUDE` 和规模 |
| v2 | Claude Sonnet | 宿主机 | ~9 分钟 | 1.0 | 自写暴力参考，交叉验证约 5,800 条随机查询 |
| v2 | 更强 Claude 模型的 v1 解重新打分 | — | ~33 分钟 | v1 上 1.0，v2 上 0.5 | 它拿到的是 v1 指令 |
| v2 | GPT-5.6 Sol（第 1 次） | **离线任务容器** | 5.0 分钟 | 0.167（1/6） | 单条用例通过 94% |
| v2 | GPT-5.6 Sol（第 2 次） | 离线任务容器 | — | 不计入 | 测试框架网络错误中断 |
| v2 | GPT-5.6 Sol（第 3 次） | **离线任务容器** | 6.0 分钟 | **1.0**（6/6） | |
| v2 | GPT-5.6 Sol（第 4 次） | **离线任务容器** | 6.1 分钟 | 0.167（1/6） | 单条用例通过 93% |

**GPT-5.6 Sol 的测试方法**：自写的最小 agent 框架（bash 和写文件两个工具，经 OpenRouter 调用，推理强度 high，上限 150 轮 / $20 / 90 分钟），模型在由环境镜像启动的断网容器（2 CPU / 4 GB）里做题，结束后导出 `/app` 按正式流程判分。3 次有效运行平均 0.44，通过率 1/3，共花费约 $5.4。

**失败原因分析**：第 1、4 次失败都来自同一类 bug，即用"对排序键取负数"实现降序，遇到字符串或 NULL 就崩溃。由于每个档位都包含带 `DESC` 的字符串或 NULL 排序键，这一个 bug 让 6 档中的 5 档归零，尽管 93–94% 的单条用例是对的。分数因此呈两极分布（0.17 或 1.0）。

**结论**：这个任务有效、公平、难以作弊。对 GPT-5.6 Sol 而言，单次通过率为 1/3，有一定难度；但 Claude Sonnet 在宿主机上一次就拿到满分，难度并不稳定。根本原因是：公平性要求 instruction 是一份完整精确的规格，而规格越精确，agent 越容易自己写暴力参考做对照测试，成功的那次 Sonnet 运行正是这么做的。它适合作为中等难度任务，难度上限需要靠第 7 节提到的"优化器交互"这类规格本身无法暴露的问题来提高。

---

## 9. 与同学方案的对比与反思

同学的任务用 gpt-5.6-sol 跑了 3 次，得分 0.769 / 0.923 / 0.769（13 组中过 10–12 组），平均 0.82，没有满分。对比后的主要体会：

1. **计分组应按相互独立的能力划分。** 同学的组（如 non-calendar、timezone-suffix）各测一个具体语义，一个 bug 通常只影响一组。我们的 6 个档位共用排序和 NULL 处理，一个排序 bug 就让 5 档归零。我们的分数因此更多在衡量"是否存在任何 bug"，而不是"完成了多少"。
2. **组更细，分数分辨率更高。** 13 组时每组约 0.077，同一模型多次运行的分数有连续起伏，说明任务处在模型能力边缘，训练信号丰富。我们 6 组且高度相关，分数在 0.17 和 1.0 之间跳动。
3. **保留的做法**：组内"全对才得分"、回归门槛、在真实离线容器里多次运行测难度，这几点两边一致。
4. **改进方向**：把计分拆成约 15 个独立能力组，单设一个"排序与 NULL"组，其他组尽量使用简单的整数排序键。改完后可以直接用已保存的 Sol 解重新打分验证效果，不必重新花钱跑模型。

## 10. 已知局限

- **固定随机种子**：每次评测用同一批查询，理论上可以跨训练回合记忆。如果每次换种子，未完全正确的解得分会随种子变化，违反"结果必须确定"的要求，所以保持固定。
- **覆盖范围**：模板之外的功能（如 `FILTER`、窗口内的 `DISTINCT` 聚合）既不要求也不测试。
- **隔离依赖 Linux 权限机制**：内核或 `setpriv` 层面的提权漏洞可以绕过。
- **计时**：只有规模档对时间敏感。20 秒的时限约为 oracle 最慢用例的 12 倍；如果某个解恰好卡在时限附近，结果可能不稳定。
- **早期 agent 实测在宿主机上进行**，与断网容器不完全相同；GPT-5.6 Sol 的实测在真实的离线容器里进行。

---

## 11. 需要你完成的事项

1. 把 `task.toml` 中的 `TODO-your-name`、`TODO-your-email` 换成真实信息。
2. 修改 `NOTES.md` 中 "How I used AI" 一节，使其符合你的实际情况。
3. 撰写 `REPORT.md`（约 500 词，必须本人撰写）。
4. 重新打包：
   ```
   cd "/Users/xizhu/Desktop/System Design/submission" && rm -f ../submission.zip && zip -qr ../submission.zip task2 -x '*/__pycache__/*' '*.DS_Store'
   ```
5. 压缩包中缺少 `task1/`，可能影响总分。
