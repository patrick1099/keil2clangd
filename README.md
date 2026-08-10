# keil2clangd — Claude Code 插件

嵌入式仓库初始化两件套:**让编辑器看懂工程**(clangd 配置),**让 git 只报代码改动**(repo-hygiene)。

## 一、生成 clangd 配置

给嵌入式 C 工程生成并校验 `.clangd` + `compile_commands.json`,让 VS Code 的 clangd 做跳转、补全和诊断。支持三种工程:

| 工程 | 识别 | 后端 | 做法 |
|---|---|---|---|
| Keil MDK | `*.uvprojx` | `Keil2Clangd.py` | 解析 XML + `.dep`,合成全部编译参数 |
| IAR EW | `*.ewp` | `Iar2Clangd.py` | 解析 XML,并**直接探测真实 IAR 编译器**拿内建宏 |
| CMake | `CMakeLists.txt` | `Cmake2Clangd.py` | 跑 configure,再让 CMake 自己产的数据库能被 clangd 找到 |

脚本负责机械解析,skill 负责流程编排与人工校验——多 target/configuration 对比、跨 target 缺失宏检测(如 target 名带 `LG048` 却漏了 `__LG048`)、`__packed`/`__align`/`__weak` 等 ARMCC 扩展兼容、Pack/CMSIS 版本校验。

## 安装(Claude Code)

```
/plugin marketplace add patrick1099/keil2clangd
/plugin install keil2clangd@keil2clangd
```

安装后在任意 Keil/IAR/CMake 工程目录里让 Claude "生成 clangd 配置",skill 即自动触发。

## 直接用脚本(不经 Claude)

```bash
# 自动识别工程类型并分发
py -3 scripts/Proj2Clangd.py -p <工程目录> --detect-only
py -3 scripts/Proj2Clangd.py -p <工程目录> -o <输出目录>

# 也可以直接点名后端
py -3 scripts/Keil2Clangd.py -p <工程目录> -o . -t <target_name>
py -3 scripts/Iar2Clangd.py  -p <工程目录> -o . -c <configuration>
py -3 scripts/Cmake2Clangd.py -p <工程目录>
```

`-h` 查看全部选项。Keil / IAR 安装路径都会缓存到 `~/.keil2clangd.json`,后续工程复用。

`--probe-args` 和 `--cmake-args` 的值以 `-` 开头,**必须用 `=` 连写**(`--cmake-args="-DFOO=BAR"`),否则 argparse 会把值当成另一个选项。

## IAR 后端为什么不猜宏

`.ewp` 里存的是 IDE 下拉框的**索引**,含义随架构和 workbench 版本变,静态映射表必然出错。所以这个后端把编译器本身当作事实来源:

- 跑 `icc<arch>.exe --predef_macros` 拿到该编译器**真实**的全部内建宏(RL78 是 300+ 条,含 `__ICCRL78__` / `__CORE__` / `__DATA_MODEL__`),写进生成的预包含头,用 `-imacros` 挂上。
- **char 有无符号**从 `__CHAR_MIN__` 反推,不猜。
- **`--core`** 靠试编译谈判:用 `GenDeviceSelect` 推出设备头(`R5F10WMG` → `ior5f10wmg.h`),逐个候选 core 编译,谁过用谁。设备头里写着 `#error "... for use with ICCRL78 option --core rl78_1 only"`,猜错不是小事。
- **`--target` triple** 按探测到的指针宽度选。必须显式给:不给的话 clang 退回宿主 triple,Windows 上那是 MSVC triple,它预声明的 `size_t` 会和 IAR 的目标位宽 `size_t` 冲突。clang 没有后端的架构(RL78/RX/STM8)用尺寸吻合的替身,替身带进来的架构身份宏和 clang 自己的 `__GNUC__`/`__clang__` 都在生成头里 `#undef` 掉。

`Ewp2Json.py` 已退役(它只认名字叫 `ICCARM` 的节点,在 RL78/RX/430 工程上会静默解析出零个宏、零条 include,却照样吐出一份看着正常的 compile_commands.json),现转发到 `Iar2Clangd.py`。

## 一个所有后端都会踩的坑:clangd 找不到配置

clangd 只在源文件自身目录和**祖先目录**里找配置,**从不看兄弟目录**。而输出通常落在 `Proj`/`build`,源码在兄弟目录 `Code`/`src`。症状很有欺骗性:**同文件跳转照常好用,跨文件跳转和查引用静默失效**。

三个后端现在都会自动检查并打印 `placement: OK` / `placement: PROBLEM`,并能在源码的最近公共祖先目录写一个指针 `.clangd`(Keil/IAR 加 `--fix-placement`,CMake 默认就做)。

## 二、repo-hygiene — 让 `git status` 只剩代码

Keil/IAR 仓库的待提交列表长期被编译残留、IDE 状态和索引配置淹没。这些噪音看着是一类,实际需要**三种互不通用**的修法,选错了不是效果差,是根本不生效、或者把同事的文件删了:

| 噪音 | 例子 | 修法 | 用错会怎样 |
|---|---|---|---|
| 未跟踪的生成物 | `Objects/`、`*.map`、`.clangd`、`~$说明书.docx` | `.gitignore` | — |
| **已跟踪**、只有本机在改 | `*.uvoptx`、`RTE_Components.h`、IAR `*.pbd` | `git update-index --skip-worktree` | `.gitignore` 对已跟踪文件**完全无效**;`git rm --cached` 会在同事下次 pull 时**删掉他们的文件** |
| 内容没变却显示"已修改" | `*.uvprojx` | `git add --renormalize` + `.gitattributes -text` | 前两种都碰不到它 |
| 只有仓库主人能判断 | `Release/Useful/*.bin` | 报告,不动 | 猜错就是丢发布固件 |

```bash
py -3 scripts/RepoHygiene.py -p <repo>                # 只扫描,默认什么都不写
py -3 scripts/RepoHygiene.py -p <父目录> --each        # 扫描其下每个仓库
py -3 scripts/RepoHygiene.py -p <repo> --apply --dry-run
py -3 scripts/RepoHygiene.py -p <repo> --apply
py -3 scripts/RepoHygiene.py -p <repo> --unfreeze [<路径>...]
```

生成的 `.gitignore` 分三段:生成块(每组规则带一句为什么)、手工增补区(旧文件里无规则覆盖的行原样搬进来,重跑不动它)、例外区(`!keil2clangd-reanchor.exe`,**必须在最后**,因为 git 按最后一条匹配的规则决定)。旧文件存 `.gitignore.bak`。

**写完用真 git 自检。** `.gitignore` 和 `.gitattributes` 都**不支持行尾注释**——`Objects/  # 输出目录` 会被 git 当成一个含空格和 `#` 的模式,匹配不到任何东西,而 git 从不报告"这条规则没匹配上任何文件"。所以自检问的不是"文件写出去了吗",是"git 现在真的忽略这些文件了吗"(`git check-ignore -z --stdin` / `git check-attr`)。

`*.uvprojx` **永不冻结**——它是真正的工程定义(源文件清单、编译宏、优化等级),改了必须提交。

## 结构

```
.claude-plugin/plugin.json       插件清单
.claude-plugin/marketplace.json  可直接作为 marketplace 安装
skills/keil2clangd/SKILL.md      skill(clangd 配置流程 + 校验)
skills/repo-hygiene/SKILL.md     skill(git 噪音治理)
scripts/RepoHygiene.py           .gitignore / skip-worktree 冻结 / 换行归一
scripts/Proj2Clangd.py           统一入口:识别工程类型并分发
scripts/k2c_common.py            共用:路径格式化 / .clangd 渲染 / 数据库写出 / 位置校验
scripts/Keil2Clangd.py           Keil 后端
scripts/Iar2Clangd.py            IAR 后端
scripts/Cmake2Clangd.py          CMake 后端
scripts/ReAnchor.py              搬家/换机修复(仅 Keil)
scripts/tests/                   239 个单元测试,py -3 -m pytest
```

## 工程搬家 / 换机(re-anchor)

生成的 `.clangd`/`compile_commands.json` 含机器/路径绑定信息,工程挪目录或换机后会失效。`scripts/ReAnchor.py` 只做外科手术式修复:重写 `directory` 和失效的绝对路径 toolchain `-I`/`-imacros`,相对 `-I`、`-D` 宏、注释和人工/AI 加的行原样保留。详见 SKILL.md 的 "Project moved / new machine" 一节。

```bash
py -3 scripts/ReAnchor.py --root <project_root>
```

同机搬家全自动;换机会探测 Keil 安装位置后重写死路径。也可用 `scripts/build_exe.bat` 打包成 `keil2clangd-reanchor.exe`,放到 `.clangd` 旁双击运行,目标机器不需要 Python/插件。**ReAnchor 只覆盖 Keil**;IAR 工程搬家直接重跑 `Iar2Clangd.py` 即可,它本来每次就重新探测。

## 已知限制:IAR 设备头里的 SFR 名字

IAR 设备头用两个厂商扩展声明特殊功能寄存器:

```c
__saddr __no_init volatile union { unsigned char P0; __BITS8 P0_bit; } @ 0xFFF00;
```

clang 解析不了 `@ 地址` 放置语法;而且就算把 `@` 去掉,**C 语言里文件作用域的匿名 union 也不会把成员导出到外层作用域**(实测 `-fms-extensions` 也不行)。所以 `P0` 这类 SFR 名字跳不了。

已做的缓解:始终发 `-ferror-limit=0`。不加的话前 19 个解析错误就会中止整个翻译单元、**什么都索引不到**;加了之后损害局限在厂商头里,clangd 又会把被包含文件的错误折叠,编辑器里只在 `#include` 行显示一个波浪线。

在一个 56 文件的 RL78 工程上实测:33 个文件(59%)除那一个头文件波浪线外完全干净,23 个文件(41%,BSP/寄存器层)仍报 SFR `use of undeclared identifier`。

## 致谢

解析脚本源自 [huiyi-li/keil2clangd](https://github.com/huiyi-li/keil2clangd),本仓库在其基础上打包为 Claude Code 插件并补充了 skill 校验流程。`.dep` 解析思路参考 [vankubo/uvConvertor](https://github.com/vankubo/uvConvertor) 与 [a3750/uvconvertor](https://github.com/a3750/uvconvertor)。许可证见 `LICENSE`。
