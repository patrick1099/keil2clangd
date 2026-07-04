# keil2clangd：吸收 `.dep` 地面真值（Hybrid-by-role）设计

> 日期：2026-07-05
> 状态：设计已批准，待写实现计划
> 范围：**仅 Keil `.uvprojx`**（IAR `.ewp` 本轮不动）
> 上游参考：[vankubo/uvConvertor](https://github.com/vankubo/uvConvertor)（C++，2.3.2 / 2025-03）、[a3750/uvconvertor](https://github.com/a3750/uvconvertor)（Rust，1.0.1 / 2025-07）

## 1. 背景与决策

keil2clangd 现状：纯 Python（`scripts/Keil2Clangd.py`，740 行），直接读 `.uvprojx` XML → 生成 `.clangd` + `compile_commands.json`，**不需要先构建工程**。独有价值：`.clangd` 生成、多 target 对比、跨 target 缺宏智能检测、ARMCC 兼容宏、IAR、CMSIS/Pack 版本解析。

撞款 uvConvertor（两版都是 C++/Rust、活跃度低）唯一比我们强的一块：解析 Keil 构建后生成的 `.dep` 文件，拿到**每个源文件真实的编译命令行**（地面真值）。

**决策：不 fork（换语言重写增量层代价过大、收益边际），只把 `.dep` 解析这一能力移植进我们的 Python。**

### 关键约束：`.dep` 会过期

`.dep` 是「上一次编译」的快照。若用户在 Keil 里**改了宏但没重新编译**，`.dep` 里仍是旧宏。若此时用 `.dep` 的宏喂 clangd，用户新宏对应的 `#ifdef` 分支不激活 → 跳转/补全失效。这是本设计必须规避的头号风险。

因此**不能按「整体信 `.dep` 还是 XML」择一**，而是**按信息角色分工**：用户会手改、且决定跳转的部分永远来自实时的 XML；`.dep` 只补那些「稍旧也不害跳转」且 XML 拿不到的部分；再加过期护栏。

## 2. 核心设计：Hybrid-by-role

每一项信息指定唯一「真相来源」：

| 信息 | 真相来源 | 理由 |
|---|---|---|
| 宏定义 `-D` | **XML（`VariousControls/Define` + 每文件/每 group 覆写）** | 用户手改、决定 `#ifdef` 跳转，必须实时 |
| 工程 include 路径 `-I` | **XML（`VariousControls/IncludePath`）** | 同上，用户会手改 |
| 编译器系统头路径 | **`.dep`（Toolchain Path → Include）** | XML 拿不到；很少变；旧了也不害跳转 |
| `--preinclude` / `-imacros` 预包含头 | **`.dep`** | XML 拿不到 |
| 实际编译的源文件清单 | **`.dep`（`F(...)` 行首）**，无 `.dep` 时回退 XML group 枚举 | 反映真正编译过的文件；文件集变化不影响「改宏跳转」 |
| CPU/arch 兼容宏、ARMCC 扩展宏 | **现有逻辑（映射表 + skill 校验）不变** | 已是差异化能力 |

**一句话**：`.dep` 是**增强层（enrichment）**，不是替换层。它只往结果里**补充** XML 拿不到的字段（系统头、预包含、真实文件清单），绝不覆盖 XML 提供的宏与工程 include。

### 过期护栏（Staleness guard）

生成前比较 `mtime(.uvprojx)` 与 `mtime(.dep)`：

- `mtime(.uvprojx) <= mtime(.dep)`：`.dep` 视为**新鲜**，正常参与增强。
- `mtime(.uvprojx) > mtime(.dep)`：`.dep` 视为**过期** → 打印醒目警告（「工程配置比上次编译新，`.dep` 已过期，本次仅用 `.uvprojx`，如需系统头/预包含请重新编译工程」）→ 本次**完全忽略 `.dep`**，退化为纯 XML 路径（等同现有行为）。

护栏保证：即便用户改了宏没重编，工具也不会用旧宏坑他。

## 3. 组件设计

在 `scripts/Keil2Clangd.py` 内新增/改动，沿用现有类风格：

### 3.1 新增 `DepParser` 类

职责单一：定位并解析一个 target 的 `.dep`，产出结构化增强数据；对「不存在 / 过期 / 解析失败」一律优雅降级返回「无增强」，绝不抛断流程。

- **定位**：`.dep` 路径 = `<uvprojx 所在目录>/<OutputDirectory>/<工程名>_<TargetName>.dep`（`OutputDirectory` 取自 XML `TargetOption/.../OutputDirectory`；缺省用 Keil 默认 `Objects/`）。找不到即返回空增强。
- **新鲜度**：对比 mtime，过期返回空增强并置 `stale=True`（供上层出警告）。
- **解析 `F(...)` 行**（移植 uvConvertor 逻辑）：
  - 每条记录形如 `F (源文件)(0xADDR)(编译参数...)`；跨行时行内 `\r` 需当分隔处理（Keil 的坑）。
  - 按 `)(` 切分：首段=源文件路径；末段=参数串，按「两个空格」再切成单参数。
  - 抽取：源文件清单、`--preinclude`/`-imacros` 预包含头、Toolchain Path→Include。
  - 参数清洗（照 clangd 需要）：丢弃 `-o`、`--depend`、`--diag_suppress=`、`--apcs=`、`--split_sections` 等；`-imacros`/`-preinclude=` 归一成 clangd 认的 `-imacros <file>`。
  - 路径 `\\` → `/`。
- **产出**（`DepEnrichment` 数据结构）：`{system_includes: [...], preinclude_files: [...], source_files: [...], stale: bool, found: bool}`。
  - 注意：**不产出宏、不产出工程 include**——这些是 XML 的地盘。

### 3.2 改动 `CompileCommandsGenerator`

- 构造时接收可选 `DepEnrichment`。
- `generate()`：宏与工程 `-I` 仍全部来自现有 XML 逻辑；若增强新鲜，额外追加 `system_includes` 的 `-I`、`preinclude_files` 的 `-imacros`；源文件清单优先用增强的 `source_files`，否则用 XML 的 `get_source_files()`。

### 3.3 改动 `ClangdGenerator`

- `CompileFlags.Add` 中的 `-D`/`-I` 仍来自 XML；若增强新鲜，追加系统头 `-I` 与 `-imacros`。ARMCC 兼容宏、Suppress 等现有行为不变。

### 3.4 改动 `main()` 与 CLI

- 默认：自动尝试定位 `.dep` 并（若新鲜）参与增强。这是**自动优先 `.dep` 增强**，但因 hybrid-by-role + 过期护栏，宏永远实时，用户无需操心。
- 新增开关：
  - `--no-dep`：完全跳过 `.dep`，强制纯 XML（排障/复现用）。
  - `--dep-path PATH`：手动指定 `.dep`（输出目录非标准时）。
- 运行日志明确打印：本次用了 `.dep`（路径）/ `.dep` 过期已忽略 / 未找到 `.dep` 走纯 XML——让用户随时知道数据来源。

### 3.5 SKILL.md 更新

- 在流程里加一段说明「双来源与过期护栏」，指导 skill 在校验阶段：
  - 若日志报 `.dep` 过期 → 提示用户「改过宏就重新编译一次再生成，或接受纯 XML 结果」。
  - 把 `.dep` 带来的系统头/预包含纳入 include 路径存在性校验。

## 4. 数据流

```
.uvprojx (XML) ──> UvprojxParser ──> {defines, project_includes, xml_source_files}   [真相：宏 + 工程 -I]
                                                     │
<out>/<proj>_<target>.dep ──> DepParser ──> DepEnrichment {system_includes,          [增强：系统头/预包含/真实文件清单]
                                              preinclude_files, source_files, stale}
                                                     │
                          mtime 护栏：.uvprojx 比 .dep 新 ⇒ 丢弃增强 + 警告
                                                     │
                                                     ▼
                       ClangdGenerator / CompileCommandsGenerator（合并：XML 宏/-I  +  新鲜增强的系统头/-imacros/文件清单）
                                                     ▼
                                          .clangd  +  compile_commands.json
                                                     ▼
                                  skill 校验（多 target 缺宏、路径存在性、ARMCC 兼容……不变）
```

## 5. 错误处理 / 降级

`.dep` 相关任何异常都**降级为纯 XML**（现有行为），并打印原因，绝不中断：

- 找不到 `.dep`：静默走纯 XML（正常场景，工程没编译过）。
- `.dep` 过期：警告 + 走纯 XML。
- `.dep` 解析失败/格式异常：警告 + 走纯 XML。
- Toolchain Path 缺失：仅跳过系统头增强，其余照常。

不变式：**任何情况下，输出的宏集合恒等于当前 `.uvprojx` 的宏集合（加 CPU/ARMCC 自动宏）**——这是「改宏必能跳转」的保证。

## 6. 测试策略

- **单测 `DepParser`**：喂样例 `.dep`（含 `-preinclude`、`-imacros`、带空格路径、`\r` 断行、Toolchain Path 行），断言抽出的系统头/预包含/文件清单正确；喂缺损/空文件断言返回空增强不抛。
- **过期护栏测**：构造 `.uvprojx` mtime 新于 `.dep` → 断言增强被丢弃且置 warning。
- **不变式测**：同一工程，`--no-dep` 与默认（有新鲜 `.dep`）两次生成，断言两者 `-D` 宏集合完全一致（只差系统头/`-imacros`/文件清单）。
- **端到端**：用仓库现有真实工程样例（若有）跑一遍，人工核对 `.clangd`。

## 7. 明确不做（YAGNI / 本轮范围外）

- IAR `.ewp` 的地面真值解析（`Ewp2Json.py` 不动）——单独一轮。
- 用 `.dep` 反推/覆盖宏——**故意不做**，正是过期风险来源。
- uvConvertor 的 `--pattern` 路径正则替换、`--extopts/--rmopts`——现有 `-a/--absolute` 与 skill 校验已覆盖主要需求，暂不引入。
- 打成 vsix。

## 8. 致谢

`.dep` 解析思路移植自 vankubo/uvConvertor（C++）与 a3750/uvconvertor（Rust），README 致谢章节补一句来源。
