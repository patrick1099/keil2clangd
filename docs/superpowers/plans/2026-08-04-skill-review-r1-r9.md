# 2026-08-04 Skill review R1–R9

来源：一次真实运行的独立复盘（目标仓库
`Commercial_Ultrasonic_Modular-open-ota`，任务「把另一个仓库的配置拉过来，release 版本」）。
用户已批准全部九项。本文件记录**每项要做什么**，供实现与验收对照。

批次划分（按风险聚类，同批共享测试）：

| 批 | 条目 | 主题 |
|---|---|---|
| 1 | R2 R3 R9 R7 | ReAnchor 写路径 + exe 交付位置 |
| 2 | R1 R4 R5 R8 | SKILL.md 路由与命令模板 |
| 3 | R6 | 新增隐藏宏扫描 |

---

## R1 — SKILL.md 加「已有配置可复用吗」判定门

**问题**：`ReAnchor` 章节在 SKILL.md 尾部，`## Pick a backend` 只有
keil/iar/cmake 三分支，没有任何从主流程指向复用路径的判定。实跑时 AI 全程
没把 ReAnchor 当候选评估过，用户误以为工具被忘了。

**做什么**：在 `## Pick a backend` 之前插入判定门，给出三条机械判据：

1. 工程文件名（`.uvprojx` / `.ewp`）是否相同；
2. 选定 target / configuration 是否相同；
3. 候选 `compile_commands.json` 的 `file` 清单在新根下是否基本存在。

三条全中 = 同工程换位置 → 拷贝 + ReAnchor。任一不中 = 重新生成，
**并且必须向用户说明为什么不能复用**（这次的缺失点就在这句话）。

---

## R2 — ReAnchor 增加 `file` 清单归属校验

**问题**：`reanchor_entries()` 只改 `directory` 和死的绝对 `-I/-imacros`，
从不读 `entry['file']`。把别的工程的 `compile_commands.json` 拷过来跑，
它会打印 `Changed: N path(s).` 并 `return 0` —— 静默给出一份错的数据库。
实测：源库 348 条里 286 条文件在目标仓库不存在。

**做什么**：新增归属校验，**在任何写入之前**执行。

- 相对 `file` 按该配置自己的 `directory` 解析（即新 root）。
- 缺失比例 > 阈值（默认 0.10）→ 打印 `WARNING: this database does not
  belong to this project`，列出样例，**不写任何文件**，非零退出。
- `--force` 越过；`--ownership-threshold` 可调。
- 正常搬家会删掉少量文件，所以用比例而非零容忍。

---

## R3 — dry-run 闸下沉进 write 函数

**问题**：`Keil2Clangd.py:834-835` 的 `write_pointer_clangd()` 在 `:839` 的
`if args.dry_run: return 0` **之前**；`Iar2Clangd.py:912-913` vs `:918` 同样。
于是 `--dry-run --fix-placement` 会真写文件，然后打印「no files written」。
`Iar2Clangd.py:881` 的 `output_dir.mkdir()` 也在 dry-run 前。
`Cmake2Clangd.py` 的顺序是对的。违反全局原则
`feedback_preview_must_share_write_path`（闸必须在写路径里）。
`grep -rn "fix.placement" tests/` 零命中 —— 该 flag 完全没有测试覆盖。

**做什么**：

- `k2c_common` 三个写函数（`ClangdDoc.write`、`write_compile_commands`、
  `write_pointer_clangd`）统一接受 `dry_run=False`；为真时只打印
  `Would generate: ...` 并返回，不落盘。这是唯一的闸。
- 两个后端把 `args.dry_run` 透传给这三个函数，删掉提前 `return 0`，
  改为结尾统一打印 `--dry-run: no files written.`。
- `Iar2Clangd` 的 `output_dir.mkdir()` 同样受闸。
- 补测试：`--dry-run --fix-placement` 不产生任何文件；
  非 dry-run 时指针内容与相对路径正确。

---

## R4 — Keil Step 2 命令模板默认带 `--fix-placement`

**问题**：SKILL.md Step 2 的模板不带该 flag，正文把它写成 PROBLEM 之后的
补救。但 Keil 的 `Proj/` 输出目录与 `Code/` 源码目录是兄弟布局，
PROBLEM 必然出现 —— 实跑时同一条命令跑了两遍。
`Keil2Clangd.py:834` 有 `not placement.ok` 守卫，布局正常时该 flag 是 no-op。

**做什么**：模板直接带上 `--fix-placement`，正文注明 placement OK 时它不动
任何文件。IAR 的 Step 2 同样处理。

---

## R5 — Keil Step 1 改用脚本输出的 target 表

**问题**：SKILL.md 让人手读 XML 枚举 target，实跑时 AI 手搓了个 PowerShell
`[xml]` 解析器。但 `Keil2Clangd.py:720-747` 早就打印全 target 宏表
（含 `<-- selected` 与跨 target WARN），且 `check_macros()` 在 `:806`、
早于 dry-run 返回点，`--dry-run` 就能拿到。IAR 那边用的是 `--list-configs`，
两边不对称。

**做什么**：Keil Step 1 改为先跑 `Keil2Clangd.py -p <dir> --dry-run`，
把脚本输出的表直接呈给用户选 target，与 IAR 的 `--list-configs` 拉平。
（R3 修好之前，`--dry-run` 不可与 `--fix-placement` 同用。）

---

## R6 — 新增 `--scan-hidden-macros`

**问题**：SKILL.md 步骤 3（标了 CRITICAL）的 3a/3d 只说「grep」，不给工具与
聚合方式。实跑时为此空转 3 次工具调用（`-Include` 缺通配、
`[System.IO.File]` 读到 Esafenet 密文、Grep 落盘后再读又是密文）。
手工聚合不可复现，与加密无关 —— 任何仓库都一样。

**做什么**：Keil/IAR 共用的 `--scan-hidden-macros`。

- 输入：刚生成的 `compile_commands.json`（源清单的权威来源）+ 全 target 宏集合。
- 输出分两栏：**未解析**（任何 target 与自动宏都没定义）与
  **已在头文件 `#define`**（如 `FM33LG0XX`、`FL_*_DRIVER_ENABLED`）。
  每项附出现次数与首个 `文件:行号`。
- 编码：源码是 GB2312，用 `gb18030` + `errors='replace'` 读。
- `#if defined(A) && defined(B)` 要抓全部标识符。
- 黄金样例（本次仓库）：必须报出 `DEF_XPQZ` / `DEF_XINGPING_CQ` /
  `DEF_RCGHDZ` / `FREEZE_CODE_ZYX` / `TASKCOVER` / `TASKIGNORE`，
  且不得把 `FM33LG0XX`、`FL_*_DRIVER_ENABLED` 列为未解析。

---

## R7 — `*.exe` 被 gitignore 时的出路

**问题**：SKILL.md 写「Ship the exe INSIDE the project (commit it)」，但目标
仓库 `.gitignore:17` 有 `*.exe`，指令默认不可执行。源库无此规则，所以那边
四件产物全部已跟踪，两边行为不一致。R9 把 exe 挪到仓库根后更显眼。

**做什么**：拷贝 exe 后自动跑 `git check-ignore`；命中就打印两条出路
（`git add -f`，或 `.gitignore` 加 `!keil2clangd-reanchor.exe` 反选），
让用户拍板，**不自动改 `.gitignore`**。SKILL.md 对应段落同步补写。

---

## R9 — exe 自动落项目根 + ReAnchor 向下递归

**问题**：exe 这次是手工从隔壁仓库 `Code/App/Proj/` 拷来的，既不自动，
位置也埋在三层目录里；而 SKILL.md 说「placed next to `.clangd`」，
`.clangd` 恰恰在最深处。

**关键约束**：`ReAnchor.py:145-147` 的 `_default_root()` 冻结时 = exe 自己所在
目录，`:179-183` 只在**该目录本身**找配置，两个都没有就 ERROR 退出，
**没有任何向下递归**。所以光把 exe 放根目录会直接坏掉。三点必须一起做：

1. **生成端**：生成成功后把 `scripts/dist/keil2clangd-reanchor.exe` 拷到项目根
   （从输出目录向上找 `.git` 判定；找不到就退化为输出目录与源码锚点的公共祖先）。
   已存在且字节相同则跳过。`--no-exe` 关闭，`--exe-dest DIR` 覆盖目标位置。
   dist 里没有 exe（未 build）时跳过并说明，不报错。
2. **ReAnchor 端**：`_default_root()` 之后改为**向下递归**找所有含
   `.clangd` / `compile_commands.json` 的目录，逐个处理。
   `reanchor_entries()` 的 `new_root` 必须取**每份配置自己所在的目录**，
   不能再用 exe 目录 —— 现在是 `entry['directory'] = new_root` 一刀切，
   直接递归会把所有 `directory` 写成仓库根，等于把配置改坏。
   递归排除 `.git` / `Objects` / `Listings` / `build` / `dist` /
   `__pycache__` / `.vs` / `.venv` / `node_modules`，并设深度上限。
3. **多配置与指针文件**：一个仓库可能有多套工程（本例 `Code/App` 与
   `Code/Boot`）；`Code/App/Code/.clangd` 是纯指针（无 `compile_commands.json`、
   无绝对 toolchain 路径）。递归要能一次处理多份，并把指针识别成 no-op
   而不是报错。

**「根目录」定义**：git 仓库根（用户在资源管理器/VS Code 里打开的那一层）。

---

## R8 — description 补触发语

**问题**：`plugin.json` / SKILL.md frontmatter 的 description 三个触发场景全是
「首次配置 / clangd 报错 / 跨文件跳转失效」，完全没有「项目搬家、换机、
另一份 checkout」—— 正文有整章 ReAnchor，触发面上却看不见。
本次是斜杠显式调用，所以没造成损失，优先级低于 R1–R4。

**做什么**：description 补入 moved project / new machine / another checkout。

---

## 收尾（插件发布纪律）

1. bump `.claude-plugin/plugin.json` 的 version（0.3.1 → 0.4.0）；
2. 子仓 commit + push 到 `github.com/patrick1099/keil2clangd`（个人身份）；
3. 金库根 `hub sync --refresh`；
4. `hub status --check` 验收。
