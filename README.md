# keil2clangd — Claude Code 插件

从 Keil MDK 的 `.uvprojx`(或 IAR 的 `.ewp`)工程文件生成并校验 `.clangd` + `compile_commands.json`,让 VS Code 的 clangd 对嵌入式 C 工程做跳转、补全和诊断。

脚本优先读取 `.uvprojx` 免编译生成；若工程已编译，自动吸收 `.dep` 的系统头/预包含/真实文件清单，并有过期检测。脚本负责解析工程文件的宏定义与 include 路径;skill 负责流程编排和人工校验——多 target 对比、跨 target 缺失宏检测(如 target 名带 `LG048` 却漏了 `__LG048`)、`__packed`/`__align`/`__weak` 等 ARMCC 扩展的 clangd 兼容处理、Pack/CMSIS 版本校验。

## 安装(Claude Code)

```
/plugin marketplace add patrick1099/keil2clangd
/plugin install keil2clangd@keil2clangd
```

安装后在任意 Keil/IAR 工程目录里让 Claude "生成 clangd 配置",skill 即自动触发。

## 直接用脚本(不经 Claude)

```bash
# Keil
py -3 scripts/Keil2Clangd.py -p <工程目录> -o . -t <target_name>
# IAR
py -3 scripts/Ewp2Json.py -p <工程目录> -o .
```

`-h` 查看全部选项。Keil 安装路径会缓存到 `~/.keil2clangd.json`,后续工程复用。

## 结构

```
.claude-plugin/plugin.json       插件清单
.claude-plugin/marketplace.json  可直接作为 marketplace 安装
skills/keil2clangd/SKILL.md      skill(流程 + 校验)
scripts/                         Keil2Clangd.py / Ewp2Json.py / Keil2Json.py
```

## 致谢

解析脚本源自 [huiyi-li/keil2clangd](https://github.com/huiyi-li/keil2clangd),本仓库在其基础上打包为 Claude Code 插件并补充了 skill 校验流程。`.dep` 解析思路参考 [vankubo/uvConvertor](https://github.com/vankubo/uvConvertor) 与 [a3750/uvconvertor](https://github.com/a3750/uvconvertor)。许可证见 `LICENSE`。
