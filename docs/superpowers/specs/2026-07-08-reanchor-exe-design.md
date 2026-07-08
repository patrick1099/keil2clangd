# Re-anchor exe 设计（项目搬家/换机后的路径重锚定）

日期：2026-07-08
状态：已批准（用户确认 B2 完整范围）

## 背景与问题

`.clangd` + `compile_commands.json` 生成后，项目一旦**挪目录**或**搬到另一台机器**，其中与位置/机器绑定的路径会失效。经 clangd 22.1.8 实测（Windows，`clangd --check`）：

| 测试 | `directory` 字段 | clangd CWD | 结果 |
|---|---|---|---|
| A | `"."`（相对） | 项目根 | ❌ 头文件解析失败 |
| B | 绝对（正确） | 无关目录 | ✅ 0 errors |
| C | 绝对（陈旧/不存在） | 项目根 | ✅（靠 CWD 兜底） |
| E | 绝对（陈旧/不存在） | 无关目录 | ❌ 失败 |
| F/G | `.clangd`-only 相对 `-I`，无 compile db | 任意 | ❌ 失败 |
| I | `.clangd` 相对 `-I` + compile db 绝对 `directory` | 无关目录 | ✅ 0 errors |

**三条硬结论**：
1. `compile_commands.json` 的 `directory` **必须是当前机器上正确的绝对路径**——相对值永远不行，这是 clangd 的硬限制，"全相对路径"方案不成立。
2. `.clangd` 里的相对 `-I` 依靠 compile db 的 `directory` 锚定解析；`directory` 陈旧时只能靠 clangd 的 CWD 兜底（VS Code 场景 CWD=工作区根，碰巧能用，但脆弱）。
3. 工具链绝对 `-I`（如 `C:/Keil_v5/...`）跨机器可能失效（盘符/版本不同），相对化救不了，只能本机重探测。

## 约束（来自 skill 的使用模型）

本 skill 的工作流是「脚本生成基线 → AI 增补审查」（SKILL.md 步骤 3–6：补隐藏宏、加 `__packed`/`__weak` 兼容宏、删失效 `-I`、加 Suppress）。因此生成物**不是纯机器产物**，任何"重跑脚本覆盖"的方案都会抹掉 AI 增补，违反使用模型。工具必须是**外科手术式**：只改必须改的路径，其余字节不动。

## 设计

### ① 定位与形态

单文件 `keil2clangd-reanchor.exe`（PyInstaller onefile），放在 `.clangd`/`compile_commands.json` 同目录（项目根）。双击或命令行运行；无参数时默认「以 exe 所在目录为项目根，原地手术」。不依赖 Python、不依赖 VS Code 插件。

> 注：脚本形态 `ReAnchor.py` 同样可直接运行（有 Python 的机器）；exe 只是它的免依赖冻结。

### ② 手术范围（核心不变式：非路径字节零改动）

| 目标 | 动作 |
|---|---|
| `compile_commands.json` 的 `directory` | 重写为当前项目根绝对路径（正斜杠） |
| 两文件中**验证失败的**绝对工具链 `-I` / `-imacros` | 用 `KeilPathResolver`（import 复用，不复制代码）本机重探 Keil，按「路径存在性」判断：存在的不碰，失效的才替换 |
| 相对 `-I`、所有 `-D`、Diagnostics、注释、AI 增补行 | **绝不触碰** |

实现方式：
- `.clangd`：**逐行文本手术**（不走 YAML 解析重排，保注释保顺序）。
- `compile_commands.json`：JSON 解析后重写，保持键序（`command`/`arguments`/`directory`/`file`）与 4 空格缩进，与生成器输出格式一致。
- 替换绝对工具链路径时同步更新 `command` 字符串与 `arguments` 数组两处（保持一致性）。

### ③ 安全阀

- 改前备份原文件为 `.clangd.bak` / `compile_commands.json.bak`（覆盖旧 bak）。
- 打印 diff 摘要：改了哪些行、旧值→新值。
- `--dry-run`：只报告不写文件。
- Keil 探测失败时**优雅降级**：只修 `directory`，工具链 `-I` 原样保留并警告（与 .dep 层同一哲学：任何缺失不中断）。
- 目标文件不存在：跳过该文件并提示，另一个照常处理。

### ④ 交付与仓库形态

- 新增 `scripts/ReAnchor.py` + `scripts/tests/test_reanchor_*.py`（stdlib unittest，延续现有测试风格）。
- 新增 `scripts/build_exe.bat`（PyInstaller 打包）；exe 二进制**不进 git**，挂 GitHub Release。
- SKILL.md 增补「项目搬家/换机」一节：何时跑 re-anchor、clangd 路径解析三条硬结论（上表）。
- 流程：本 feature 分支 + TDD + PR。

## 不做的事（YAGNI）

- 不做「全相对路径」（实测不成立，见结论 1）。
- 不做 B3（Keil `-I` 移到 clangd 用户全局配置）——更大的架构改动，另行立项。
- 不重跑生成脚本、不重排/规范化文件其余内容。
- IAR（.ewp）不在本轮范围。

## 测试要点

- `directory` 重写：陈旧绝对路径 → 当前根；已正确时不改（幂等）。
- 工具链 `-I`：存在→不碰；失效且探测到 Keil→替换；失效且探测不到→保留+警告。
- 不变式：除被替换路径行外，`.clangd` 其余行（含注释、AI 增补宏）逐字节相同。
- `command` 与 `arguments` 同步更新一致。
- `--dry-run` 不落盘；`.bak` 生成正确。
- CLI 端到端（subprocess，沿用现有 e2e 风格）。
