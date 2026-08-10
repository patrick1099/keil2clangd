---
name: repo-hygiene
description: Make `git status` in a firmware repo show code changes only — generate a sectioned .gitignore, freeze IDE/user-state files that are already tracked, and fix files reported modified whose content is byte-identical. Use when a Keil/IAR repo's pending-changes list is full of build output, *.uvoptx, *.uvguix, RTE_Components.h, .clangd / compile_commands.json or ~$ Office lock files; when a hand-maintained .gitignore has grown into dozens of literal dead paths; when a file keeps showing as modified but `git diff` is empty; or when the user asks to stop tracking changes to a file WITHOUT deleting it from the repo.
---

# Repo hygiene — 让待提交列表只剩代码

四类噪音，**三种不同的修法**。选错了不是效果差，是根本不起作用，或者把同事的文件删了。

| 噪音 | 例子 | 正确修法 | 用错会怎样 |
|---|---|---|---|
| 未跟踪的生成物 | `Objects/`、`*.map`、`.clangd`、`~$说明书.docx` | `.gitignore` | — |
| **已跟踪**、只有本机在改 | `*.uvoptx`、`RTE_Components.h`、IAR `*.pbd` | `git update-index --skip-worktree` | 写 `.gitignore` **完全无效**;`git rm --cached` 会在同事下次 pull 时**删掉他们的文件** |
| 内容没变却显示"已修改" | `*.uvprojx` | `git add --renormalize` + `.gitattributes` | 前两种都碰不到它 |
| 只有仓库主人能判断 | `Release/Useful/*.bin` | 报告,不动 | 猜错就是丢发布固件 |

**第二行是这个 skill 存在的主要理由。** 用户说"停止跟踪"时,九成指的是 skip-worktree(文件留在仓库里,只是本机改动不上报),不是 `git rm --cached`(从仓库删除)。**动手前先确认是哪一个。**

## 用法

```powershell
# 只扫描,什么都不写 —— 默认行为,先看报告
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/RepoHygiene.py" -p <repo>

# 扫描 <父目录> 下的每一个仓库
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/RepoHygiene.py" -p <父目录> --each

# 预演真实写入(走同一条写路径,只是不落盘)
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/RepoHygiene.py" -p <repo> --apply --dry-run

# 执行,也可以只挑一样做
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/RepoHygiene.py" -p <repo> --apply
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/RepoHygiene.py" -p <repo> --write-gitignore
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/RepoHygiene.py" -p <repo> --freeze
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/RepoHygiene.py" -p <repo> --renormalize

# 解冻(不给路径 = 全部解冻)
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/RepoHygiene.py" -p <repo> --unfreeze [<路径>...]
```

报告分五段,`[1]`~`[3]` 是三种修法各自负责的部分,`[4]` 是工具拒绝替人决定的,`[5]` 是现有
`.gitignore` 的体检。**先把报告给用户看,再问要不要执行。**

## 报告里必须转述给用户的三件事

1. **`[4] 只有你能判断的`** —— 逐条念给用户听,不要替他决定。典型的是
   `Release/Useful/*.bin`:可能是故意提交的发布固件,也可能是编译残留。
2. **`[5]` 里的危险规则** —— `*.py`、`*.0`、`CMakeLists.txt` 这类。工具会把它们从
   生成文件里剔除并列出来,但**剔除一条别人当初特意加的规则,必须让用户点头**。
3. **`[2]` 里"此刻就有未提交改动"的那些** —— 冻结后这些改动会从 `git status` 消失。
   盘上内容一个字节不变,但用户要知道自己看不见它们了。

## skip-worktree:三件必须一起说的事

1. **标记存在 `.git/index` 里,是每个 clone 各自的。** 不会推给同事,也不会被别的
   clone 或 worktree 继承 —— 换一份检出就要重跑一次。同一个项目有几份检出,就跑几次。
2. **同事真改了被冻结的文件时,`git pull` 会报**
   `Your local changes to the following files would be overwritten`。
   解法:`--unfreeze <文件>` → 拉 → 重新 `--freeze`。
3. **想主动提交某个冻结文件的改动**:`--unfreeze` 它 → `git add`/`commit` → 再 `--freeze`。

## 生成的 .gitignore 长什么样

三段固定顺序,重复运行不会长胖:

```
<生成块>            每条规则带一句"为什么" —— 说不出理由的行就是文件失控的起点
# ==== 手工增补 ====   工具永不改动;旧文件里无规则覆盖的行原样搬进这里
# ==== 例外 ====       !keil2clangd-reanchor.exe,必须在最后
```

- **例外必须在最后**:git 按**最后一条**匹配的规则决定。`!foo.exe` 写在手工区的
  `*.exe` 上面会被它悄悄抵消。
- **在文件末尾追加的行会被救回手工区** —— 追加在 EOF 是最自然的动作,而 EOF 在例外块
  下面;直接丢掉就是静默数据丢失。
- 旧文件写到 `.gitignore.bak`。

## 两条铁律

- **`.gitignore` 对已跟踪的文件毫无作用。** 报告 `[5]` 末尾的"规则被已跟踪文件架空"
  就是在说这个:规则写了也不生效,得靠 `[2]` 的冻结。别在这上面浪费一轮。
- **不要自己拼 `git rm --cached`。** 它会在同事下次 pull 时删掉他们本地的文件。用户
  明确要求"从仓库里删掉"时才用,而且要先说清后果。

## 改规则表

规则都在 `scripts/RepoHygiene.py` 顶部的表里,每条带一句理由:

| 表 | 管什么 |
|---|---|
| `IGNORE_SECTIONS` | 写进 `.gitignore` 的规则,按段落分组 |
| `NEGATIONS` | 必须跟着仓库走的例外,渲染在文件最后 |
| `FREEZE_RULES` | 匹配到的**已跟踪**文件建议 skip-worktree |
| `REVIEW_RULES` | 匹配到就交给用户判断,工具不动 |
| `DANGEROUS_LINES` | 旧 `.gitignore` 里遇到就剔除并报警的行 |
| `GITATTRIBUTES_BINARYISH` | 标 `-text`,防幻影修改复发 |

加规则时**顺手加一条测试**:`scripts/tests/test_repo_hygiene.py`。
`test_every_rule_carries_a_reason` 会强制每条规则带注释。

`*.uvprojx` **永远不要**进 `FREEZE_RULES` —— 它是真正的工程定义(源文件清单、编译宏),
改了必须提交。它显示"已修改"却没有 diff,是换行问题,归 `[3]` 管。
