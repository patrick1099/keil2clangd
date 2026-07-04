# `.dep` 吸收（Hybrid-by-role）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 uvConvertor 的 Keil `.dep` 地面真值解析作为「增强层」移植进 `scripts/Keil2Clangd.py`，只补充 XML 拿不到的系统头/预包含头/真实文件清单，宏与工程 `-I` 恒来自实时 `.uvprojx`，并加过期护栏。

**Architecture:** 新增纯函数 `_parse_dep_text` 和 `DepParser` 类产出 `DepEnrichment`；两个生成器（`ClangdGenerator`、`CompileCommandsGenerator`）接收可选 enrichment，仅在「found 且 not stale」时追加系统头 `-I` / `-imacros` 预包含，并可用真实源文件清单替换 XML 枚举；`main()` 默认自动定位 `.dep`，新增 `--no-dep` / `--dep-path`。

**Tech Stack:** Python 3.9 标准库（`xml.etree.ElementTree`、`re`、`pathlib`、`dataclasses`、`tempfile`）；测试用 stdlib `unittest`（零外部依赖，`python3 -m unittest`）。

## Global Constraints

- **零外部依赖**：只用 Python 标准库，测试用 `unittest`，不得引入 pytest/pip 包。
- **不变式**：任何情况下输出的 `-D` 宏集合恒等于当前 `.uvprojx` 的宏集合（加 CPU/ARMCC 自动宏）；`.dep` 绝不提供或覆盖 `-D` 与工程 `-I`。
- **优雅降级**：`.dep` 缺失/过期/解析失败一律退化为纯 XML，打印原因，绝不抛异常中断。
- **范围**：仅 Keil `.uvprojx`；不动 `Ewp2Json.py`（IAR）。
- 所有路径统一正斜杠；沿用现有类风格与 `_format_path` 辅助函数。
- 预包含头映射为 clangd 的 `-imacros`（跟随 uvConvertor 的选择，避免重复定义报错）。
- 所有新代码写入 `scripts/Keil2Clangd.py`；测试与 fixture 放 `scripts/tests/`。

---

### Task 1: `DepEnrichment` 数据结构 + `_parse_dep_text` 纯解析函数

**Files:**
- Modify: `scripts/Keil2Clangd.py`（顶部 import 增加 `re` 已有、`dataclasses`；在 `KeilPathResolver` 之前插入新代码）
- Create: `scripts/tests/__init__.py`（空文件）
- Create: `scripts/tests/test_dep_parser.py`

**Interfaces:**
- Produces:
  - `@dataclass class DepEnrichment` 字段：`found: bool=False`、`stale: bool=False`、`dep_path: Optional[Path]=None`、`system_includes: List[Path]=[]`、`preinclude_files: List[Path]=[]`、`source_files: List[Path]=[]`
  - `_parse_dep_text(text: str) -> dict` 返回 `{"system_includes": List[str], "preinclude_files": List[str], "source_files": List[str]}`（原始字符串、已转正斜杠、按出现顺序去重）

- [ ] **Step 1: 写失败测试**

创建 `scripts/tests/__init__.py`（空）。创建 `scripts/tests/test_dep_parser.py`：

```python
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import Keil2Clangd as k2c

SAMPLE_DEP = r"""Dependencies for Project 'App', Target 'App': (DO NOT MODIFY !)
Toolchain Path:  D:\Keil_v5\ARM\ARMCC\Bin
F (.\User\main.c)(0x5F3A1B2C)(--c99 -c --cpu Cortex-M3 -D__DEBUG  -I.\User  -I..\bsp  --preinclude .\User\preinc.h  -o .\Objects\main.o  --depend .\Objects\main.d)
I (.\User\stm32.h)(0x5F000000)
F (.\bsp\led.c)(0x5F3A1B2D)(--c99 -c --cpu Cortex-M3 -D__DEBUG  -I.\User  --preinclude .\User\preinc.h  -o .\Objects\led.o)
"""


class TestParseDepText(unittest.TestCase):
    def test_extracts_source_files_in_order(self):
        r = k2c._parse_dep_text(SAMPLE_DEP)
        self.assertEqual(r["source_files"], ["./User/main.c", "./bsp/led.c"])

    def test_extracts_preinclude_deduped(self):
        r = k2c._parse_dep_text(SAMPLE_DEP)
        self.assertEqual(r["preinclude_files"], ["./User/preinc.h"])

    def test_toolchain_path_becomes_include(self):
        r = k2c._parse_dep_text(SAMPLE_DEP)
        self.assertEqual(r["system_includes"], ["D:/Keil_v5/ARM/ARMCC/Include"])

    def test_ignores_i_dependency_lines(self):
        r = k2c._parse_dep_text(SAMPLE_DEP)
        self.assertNotIn("./User/stm32.h", r["source_files"])

    def test_empty_text_returns_empty_lists(self):
        r = k2c._parse_dep_text("")
        self.assertEqual(r, {"system_includes": [], "preinclude_files": [], "source_files": []})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

Run: `cd scripts && python3 -m unittest tests.test_dep_parser -v`
Expected: FAIL，`AttributeError: module 'Keil2Clangd' has no attribute '_parse_dep_text'`

- [ ] **Step 3: 实现**

在 `scripts/Keil2Clangd.py` 顶部 import 段追加：

```python
from dataclasses import dataclass, field
from typing import List, Optional
```

在 `# KeilPathResolver` 分隔注释之前插入：

```python
# ---------------------------------------------------------------------------
# .dep enrichment (ground-truth from Keil build output)
# ---------------------------------------------------------------------------

@dataclass
class DepEnrichment:
    """Supplementary build facts parsed from a Keil .dep file.

    Only fields that .uvprojx XML cannot provide. Never carries -D macros or
    project -I paths — those stay sourced from live XML.
    """
    found: bool = False
    stale: bool = False
    dep_path: Optional[Path] = None
    system_includes: List[Path] = field(default_factory=list)
    preinclude_files: List[Path] = field(default_factory=list)
    source_files: List[Path] = field(default_factory=list)


_F_LINE_RE = re.compile(r'^F \((?P<file>[^)]+)\)')
_PREINCLUDE_RE = re.compile(
    r'(?:--preinclude|-imacros)\s+"?(?P<a>[^"\s)]+)'
    r'|-preinclude="?(?P<b>[^"\s)]+)')
_TOOLCHAIN_RE = re.compile(r'^Toolchain Path:\s*(?P<p>.+?)\s*$')


def _dedup(seq):
    seen = set()
    out = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _parse_dep_text(text):
    """Parse raw .dep text into supplementary build facts (raw strings).

    Returns dict with system_includes / preinclude_files / source_files,
    forward-slashed and order-preserving deduped. Does NOT extract -D/-I.
    """
    sources = []
    preincludes = []
    sysincs = []
    for raw_line in text.splitlines():
        line = raw_line.replace('\\', '/').rstrip('\r')

        tc = _TOOLCHAIN_RE.match(line)
        if tc:
            p = tc.group('p').rstrip('/')
            # .../Bin -> .../Include
            for tail in ('/Bin', '/bin'):
                if p.endswith(tail):
                    p = p[: -len(tail)] + '/Include'
                    break
            sysincs.append(p)
            continue

        fm = _F_LINE_RE.match(line)
        if fm:
            sources.append(fm.group('file').strip())
            for m in _PREINCLUDE_RE.finditer(line):
                preincludes.append(m.group('a') or m.group('b'))

    return {
        "system_includes": _dedup(sysincs),
        "preinclude_files": _dedup(preincludes),
        "source_files": _dedup(sources),
    }
```

（注：`re` 已在文件顶部导入。）

- [ ] **Step 4: 运行确认通过**

Run: `cd scripts && python3 -m unittest tests.test_dep_parser -v`
Expected: PASS（5 tests）

- [ ] **Step 5: 提交**

```bash
cd /Users/xu/MyDocuments/my-repos/keil2clangd
git add scripts/Keil2Clangd.py scripts/tests/__init__.py scripts/tests/test_dep_parser.py
git commit -m "feat: add DepEnrichment + _parse_dep_text pure parser

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: `UvprojxParser.get_output_dir()` 与 `project_name`

**Files:**
- Modify: `scripts/Keil2Clangd.py`（`UvprojxParser` 类内，`get_source_files` 之后）
- Test: `scripts/tests/test_uvprojx_output.py`
- Create: `scripts/tests/fixtures/sample.uvprojx`

**Interfaces:**
- Consumes: `UvprojxParser(file_path, target_name=None)`（已存在）
- Produces:
  - `UvprojxParser.get_output_dir() -> str`（相对目录，正斜杠，无 `.dep` 时用于定位；缺省 `"Objects"`）
  - `UvprojxParser.project_name -> str`（property，等于 uvprojx 文件名去扩展名）

- [ ] **Step 1: 写 fixture 与失败测试**

创建 `scripts/tests/fixtures/sample.uvprojx`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<Project>
  <Targets>
    <Target>
      <TargetName>App</TargetName>
      <TargetOption>
        <TargetCommonOption>
          <OutputDirectory>.\Objects\</OutputDirectory>
        </TargetCommonOption>
      </TargetOption>
      <TargetArmAds>
        <ArmAdsMisc><AdsCpuType>"Cortex-M3"</AdsCpuType></ArmAdsMisc>
        <Cads>
          <VariousControls>
            <Define>__DEBUG, USE_HAL</Define>
            <IncludePath>.\User;..\bsp</IncludePath>
          </VariousControls>
        </Cads>
        <uAC6>0</uAC6>
      </TargetArmAds>
      <Groups>
        <Group>
          <Files>
            <File><FilePath>.\User\main.c</FilePath></File>
            <File><FilePath>.\bsp\led.c</FilePath></File>
          </Files>
        </Group>
      </Groups>
    </Target>
  </Targets>
</Project>
```

创建 `scripts/tests/test_uvprojx_output.py`：

```python
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import Keil2Clangd as k2c

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "sample.uvprojx")


class TestOutputDir(unittest.TestCase):
    def setUp(self):
        self.parser = k2c.UvprojxParser(FIX)

    def test_output_dir_normalized(self):
        self.assertEqual(self.parser.get_output_dir(), "./Objects")

    def test_project_name(self):
        self.assertEqual(self.parser.project_name, "sample")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

Run: `cd scripts && python3 -m unittest tests.test_uvprojx_output -v`
Expected: FAIL，`AttributeError: 'UvprojxParser' object has no attribute 'get_output_dir'`

- [ ] **Step 3: 实现**

在 `UvprojxParser.get_source_files` 方法之后追加：

```python
    @property
    def project_name(self):
        return self.file_path.stem

    def get_output_dir(self):
        """Relative build output dir (forward slashes). Fallback: 'Objects'."""
        elem = self.target.find('.//TargetOption/TargetCommonOption/OutputDirectory')
        if elem is not None and elem.text and elem.text.strip():
            d = elem.text.strip().replace('\\', '/').rstrip('/')
            return d if d else "Objects"
        return "Objects"
```

- [ ] **Step 4: 运行确认通过**

Run: `cd scripts && python3 -m unittest tests.test_uvprojx_output -v`
Expected: PASS（2 tests）

- [ ] **Step 5: 提交**

```bash
cd /Users/xu/MyDocuments/my-repos/keil2clangd
git add scripts/Keil2Clangd.py scripts/tests/test_uvprojx_output.py scripts/tests/fixtures/sample.uvprojx
git commit -m "feat: UvprojxParser.get_output_dir + project_name

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: `DepParser`（定位 + 过期护栏 + 优雅降级）

**Files:**
- Modify: `scripts/Keil2Clangd.py`（`_parse_dep_text` 之后追加 `DepParser` 类）
- Test: `scripts/tests/test_dep_locate.py`

**Interfaces:**
- Consumes: `DepEnrichment`、`_parse_dep_text`、`UvprojxParser`（`project_root`、`get_output_dir`、`project_name`、`get_target_name`）
- Produces:
  - `DepParser(uvprojx_parser, dep_path_override: Optional[str]=None)`
  - `DepParser.locate() -> Optional[Path]`：override 优先；否则 `<project_root>/<output_dir>/<project_name>_<target>.dep`；不存在返回 `None`
  - `DepParser.parse() -> DepEnrichment`：编排 locate→过期检查(mtime)→读文件→`_parse_dep_text`→路径解析。任何异常返回 `DepEnrichment(found=False)`。过期返回 `DepEnrichment(found=True, stale=True, dep_path=...)`。

- [ ] **Step 1: 写失败测试**

创建 `scripts/tests/test_dep_locate.py`：

```python
import os
import sys
import time
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import Keil2Clangd as k2c

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "sample.uvprojx")

DEP_BODY = (
    "Toolchain Path:  D:\\Keil_v5\\ARM\\ARMCC\\Bin\n"
    "F (.\\User\\main.c)(0x1)(--c99 -c --preinclude .\\User\\preinc.h -o x.o)\n"
)


class TestDepParser(unittest.TestCase):
    def _project(self, tmp):
        """Copy sample.uvprojx into tmp dir as proj.uvprojx, return parser."""
        proj = Path(tmp) / "proj.uvprojx"
        proj.write_text(Path(FIX).read_text(encoding="utf-8"), encoding="utf-8")
        return k2c.UvprojxParser(str(proj))

    def _write_dep(self, tmp):
        objs = Path(tmp) / "Objects"
        objs.mkdir(exist_ok=True)
        dep = objs / "proj_App.dep"
        dep.write_text(DEP_BODY, encoding="utf-8")
        return dep

    def test_missing_dep_returns_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser = self._project(tmp)
            enr = k2c.DepParser(parser).parse()
            self.assertFalse(enr.found)
            self.assertFalse(enr.stale)

    def test_fresh_dep_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser = self._project(tmp)
            dep = self._write_dep(tmp)
            # make dep newer than uvprojx
            future = time.time() + 10
            os.utime(str(dep), (future, future))
            enr = k2c.DepParser(parser).parse()
            self.assertTrue(enr.found)
            self.assertFalse(enr.stale)
            names = [p.name for p in enr.source_files]
            self.assertIn("main.c", names)
            self.assertIn("preinc.h", [p.name for p in enr.preinclude_files])
            self.assertTrue(any("Include" in str(p) for p in enr.system_includes))

    def test_stale_dep_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser = self._project(tmp)
            dep = self._write_dep(tmp)
            past = time.time() - 100
            os.utime(str(dep), (past, past))  # dep older than uvprojx
            enr = k2c.DepParser(parser).parse()
            self.assertTrue(enr.found)
            self.assertTrue(enr.stale)
            self.assertEqual(enr.source_files, [])

    def test_override_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser = self._project(tmp)
            custom = Path(tmp) / "custom.dep"
            custom.write_text(DEP_BODY, encoding="utf-8")
            future = time.time() + 10
            os.utime(str(custom), (future, future))
            enr = k2c.DepParser(parser, dep_path_override=str(custom)).parse()
            self.assertTrue(enr.found)
            self.assertFalse(enr.stale)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

Run: `cd scripts && python3 -m unittest tests.test_dep_locate -v`
Expected: FAIL，`AttributeError: module 'Keil2Clangd' has no attribute 'DepParser'`

- [ ] **Step 3: 实现**

在 `_parse_dep_text` 之后追加：

```python
class DepParser:
    """Locate and parse a target's Keil .dep, producing a DepEnrichment.

    Degrades gracefully: missing/unreadable .dep -> found=False; a .dep older
    than the .uvprojx -> found=True, stale=True (caller ignores its data).
    """

    def __init__(self, uvprojx_parser, dep_path_override=None):
        self.p = uvprojx_parser
        self.override = dep_path_override

    def locate(self):
        if self.override:
            cand = Path(self.override)
            return cand if cand.exists() else None
        out_dir = self.p.get_output_dir()
        name = "{0}_{1}.dep".format(self.p.project_name, self.p.get_target_name())
        cand = (self.p.project_root / out_dir / name)
        return cand if cand.exists() else None

    def parse(self):
        try:
            dep_path = self.locate()
            if dep_path is None:
                return DepEnrichment(found=False)

            uv_mtime = self.p.file_path.stat().st_mtime
            dep_mtime = dep_path.stat().st_mtime
            if uv_mtime > dep_mtime:
                return DepEnrichment(found=True, stale=True, dep_path=dep_path)

            raw = _parse_dep_text(dep_path.read_text(encoding="utf-8", errors="ignore"))
            root = self.p.project_root
            return DepEnrichment(
                found=True,
                stale=False,
                dep_path=dep_path,
                system_includes=[Path(s) for s in raw["system_includes"]],
                preinclude_files=[(root / f).resolve() for f in raw["preinclude_files"]],
                source_files=[(root / f).resolve() for f in raw["source_files"]],
            )
        except Exception as exc:  # never break the main flow
            print("WARNING: .dep parse failed ({0}); using .uvprojx only.".format(exc))
            return DepEnrichment(found=False)
```

- [ ] **Step 4: 运行确认通过**

Run: `cd scripts && python3 -m unittest tests.test_dep_locate -v`
Expected: PASS（4 tests）

- [ ] **Step 5: 提交**

```bash
cd /Users/xu/MyDocuments/my-repos/keil2clangd
git add scripts/Keil2Clangd.py scripts/tests/test_dep_locate.py
git commit -m "feat: DepParser with locate + staleness guard + graceful degrade

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: `CompileCommandsGenerator` 合并 enrichment

**Files:**
- Modify: `scripts/Keil2Clangd.py`（`CompileCommandsGenerator.__init__` 与 `generate`）
- Test: `scripts/tests/test_compile_commands_merge.py`

**Interfaces:**
- Consumes: `DepEnrichment`、`UvprojxParser`、`_format_path`
- Produces: `CompileCommandsGenerator(parser, keil_resolver, use_absolute=False, base_dir=None, enrichment=None)`；`generate()` 在 enrichment `found and not stale` 时：源文件清单优先用 `enrichment.source_files`（非空时）；每条命令追加 `-I<system_include>`（与现有去重）和 `-imacros <preinclude>`。宏与工程 `-I` 不变。

- [ ] **Step 1: 写失败测试**

创建 `scripts/tests/test_compile_commands_merge.py`：

```python
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import Keil2Clangd as k2c

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "sample.uvprojx")


class _NoKeil:
    def found(self):
        return False


def _defines(entry):
    return sorted(a for a in entry["arguments"] if a.startswith("-D"))


class TestCompileCommandsMerge(unittest.TestCase):
    def setUp(self):
        self.parser = k2c.UvprojxParser(FIX)
        self.base = Path(FIX).parent

    def _gen(self, enrichment):
        return k2c.CompileCommandsGenerator(
            self.parser, _NoKeil(), use_absolute=True,
            base_dir=self.base, enrichment=enrichment).generate()

    def test_defines_invariant_with_and_without_dep(self):
        plain = self._gen(None)
        enr = k2c.DepEnrichment(
            found=True, stale=False,
            system_includes=[Path("/opt/keil/Include")],
            preinclude_files=[self.base / "User" / "preinc.h"],
            source_files=[self.base / "User" / "main.c"])
        enriched = self._gen(enr)
        self.assertEqual(_defines(plain[0]), _defines(enriched[0]))

    def test_enrichment_adds_system_include_and_imacros(self):
        enr = k2c.DepEnrichment(
            found=True, stale=False,
            system_includes=[Path("/opt/keil/Include")],
            preinclude_files=[self.base / "User" / "preinc.h"],
            source_files=[self.base / "User" / "main.c"])
        e = self._gen(enr)[0]
        self.assertTrue(any(a.startswith("-I") and "Include" in a for a in e["arguments"]))
        self.assertIn("-imacros", e["arguments"])

    def test_stale_enrichment_ignored(self):
        plain = self._gen(None)
        stale = self._gen(k2c.DepEnrichment(found=True, stale=True))
        self.assertEqual(len(plain), len(stale))
        self.assertFalse(any("-imacros" in a for e in stale for a in e["arguments"]))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

Run: `cd scripts && python3 -m unittest tests.test_compile_commands_merge -v`
Expected: FAIL，`TypeError: __init__() got an unexpected keyword argument 'enrichment'`

- [ ] **Step 3: 实现**

改 `CompileCommandsGenerator.__init__` 签名与 `generate()`：

```python
    def __init__(self, parser, keil_resolver, use_absolute=False, base_dir=None,
                 enrichment=None):
        self.parser = parser
        self.keil = keil_resolver
        self.use_absolute = use_absolute
        self.base_dir = Path(base_dir).resolve() if base_dir else Path.cwd().resolve()
        self.enrichment = enrichment
```

在 `generate()` 中，`source_files = self.parser.get_source_files()` 一行改为：

```python
        source_files = self.parser.get_source_files()
        enr = self.enrichment
        use_enr = bool(enr and enr.found and not enr.stale)
        if use_enr and enr.source_files:
            source_files = enr.source_files
```

在 Keil includes 追加块之后（`base_args` 组装完、进入 `for sf in source_files` 循环之前）插入：

```python
        # .dep enrichment: compiler system includes (XML can't provide these)
        if use_enr:
            existing = {a for a in base_args if a.startswith("-I")}
            for inc in enr.system_includes:
                formatted = _format_path(inc, self.base_dir, self.use_absolute)
                flag = f"-I{formatted}"
                if flag not in existing:
                    base_args.append(flag)
                    existing.add(flag)
```

把每文件命令组装改为把 `-imacros` 预包含追加进 per-file 参数（宏保持不变，只加预包含头）。将循环体替换为：

```python
        preinclude_args = []
        if use_enr:
            for pf in enr.preinclude_files:
                formatted = _format_path(pf, self.base_dir, self.use_absolute)
                preinclude_args += ["-imacros", formatted]

        entries = []
        for sf in source_files:
            file_str = _format_path(sf, self.base_dir, self.use_absolute)
            file_args = base_args + preinclude_args
            command = f"{compiler} -c {file_str} " + " ".join(file_args)
            entry = {
                "command": command,
                "arguments": [compiler, "-c", file_str] + file_args,
                "directory": dir_str,
                "file": file_str,
            }
            entries.append(entry)

        return entries
```

- [ ] **Step 4: 运行确认通过**

Run: `cd scripts && python3 -m unittest tests.test_compile_commands_merge -v`
Expected: PASS（3 tests）

- [ ] **Step 5: 提交**

```bash
cd /Users/xu/MyDocuments/my-repos/keil2clangd
git add scripts/Keil2Clangd.py scripts/tests/test_compile_commands_merge.py
git commit -m "feat: merge .dep enrichment into CompileCommandsGenerator

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: `ClangdGenerator` 合并 enrichment

**Files:**
- Modify: `scripts/Keil2Clangd.py`（`ClangdGenerator.__init__` 与 `generate`）
- Test: `scripts/tests/test_clangd_merge.py`

**Interfaces:**
- Consumes: `DepEnrichment`、`_format_path`
- Produces: `ClangdGenerator(parser, keil_resolver, use_absolute=False, base_dir=None, enrichment=None)`；`generate()` 在 enrichment 新鲜时，于 `Remove:` 之前追加系统头 `-I` 行与 `-imacros` 行。`-D` 宏行不变。

- [ ] **Step 1: 写失败测试**

创建 `scripts/tests/test_clangd_merge.py`：

```python
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import Keil2Clangd as k2c

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "sample.uvprojx")


class _NoKeil:
    def found(self):
        return False


class TestClangdMerge(unittest.TestCase):
    def setUp(self):
        self.parser = k2c.UvprojxParser(FIX)
        self.base = Path(FIX).parent

    def _yaml(self, enrichment):
        return k2c.ClangdGenerator(
            self.parser, _NoKeil(), use_absolute=True,
            base_dir=self.base, enrichment=enrichment).generate()

    def test_defines_present_regardless(self):
        for enr in (None, k2c.DepEnrichment(
                found=True, stale=False,
                system_includes=[Path("/opt/keil/Include")],
                preinclude_files=[self.base / "User" / "preinc.h"])):
            y = self._yaml(enr)
            self.assertIn("-D__DEBUG", y)
            self.assertIn("-DUSE_HAL", y)

    def test_enrichment_adds_system_include_and_imacros(self):
        enr = k2c.DepEnrichment(
            found=True, stale=False,
            system_includes=[Path("/opt/keil/Include")],
            preinclude_files=[self.base / "User" / "preinc.h"])
        y = self._yaml(enr)
        self.assertIn("Include", y)
        self.assertIn("-imacros", y)

    def test_stale_enrichment_ignored(self):
        y = self._yaml(k2c.DepEnrichment(found=True, stale=True))
        self.assertNotIn("-imacros", y)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

Run: `cd scripts && python3 -m unittest tests.test_clangd_merge -v`
Expected: FAIL，`TypeError: __init__() got an unexpected keyword argument 'enrichment'`

- [ ] **Step 3: 实现**

改 `ClangdGenerator.__init__` 增加 `enrichment=None` 参数并 `self.enrichment = enrichment`（与 Task 4 同形）。

在 `generate()` 里、`# Remove flags` 那行（`lines.append("  Remove:")`）**之前**插入：

```python
        # .dep enrichment: system includes + forced preinclude macros
        enr = self.enrichment
        if enr and enr.found and not enr.stale:
            existing = {ln.strip() for ln in lines}
            if enr.system_includes:
                lines.append("    # Compiler system headers (from .dep)")
                for inc in enr.system_includes:
                    formatted = _format_path(inc, self.base_dir, self.use_absolute)
                    flag = f"    - -I{formatted}"
                    if flag.strip() not in existing:
                        lines.append(flag)
            if enr.preinclude_files:
                lines.append("    # Preinclude headers (from .dep)")
                for pf in enr.preinclude_files:
                    formatted = _format_path(pf, self.base_dir, self.use_absolute)
                    lines.append(f"    - -imacros")
                    lines.append(f"    - {formatted}")
```

- [ ] **Step 4: 运行确认通过**

Run: `cd scripts && python3 -m unittest tests.test_clangd_merge -v`
Expected: PASS（3 tests）

- [ ] **Step 5: 提交**

```bash
cd /Users/xu/MyDocuments/my-repos/keil2clangd
git add scripts/Keil2Clangd.py scripts/tests/test_clangd_merge.py
git commit -m "feat: merge .dep enrichment into ClangdGenerator

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: `main()` 接线 + CLI `--no-dep` / `--dep-path` + 日志

**Files:**
- Modify: `scripts/Keil2Clangd.py`（`main()`）
- Test: `scripts/tests/test_cli_e2e.py`

**Interfaces:**
- Consumes: `DepParser`、`DepEnrichment`、两个 generator 的 `enrichment=` 参数
- Produces: CLI 新增 `--no-dep`（跳过 `.dep`）与 `--dep-path PATH`（指定 `.dep`）；`main()` 默认构造 `DepParser` 并把 enrichment 传给两个 generator；按 enrichment 状态打印来源日志（used / stale-ignored / not-found）。

- [ ] **Step 1: 写失败的端到端测试**

创建 `scripts/tests/test_cli_e2e.py`：

```python
import os
import sys
import time
import json
import shutil
import tempfile
import unittest
import subprocess
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "Keil2Clangd.py")
FIX = os.path.join(HERE, "fixtures", "sample.uvprojx")

DEP_BODY = (
    "Toolchain Path:  D:\\Keil_v5\\ARM\\ARMCC\\Bin\n"
    "F (.\\User\\main.c)(0x1)(--c99 -c --preinclude .\\User\\preinc.h -o x.o)\n"
)


def run(project_dir, *extra):
    cmd = [sys.executable, SCRIPT, "-p", str(project_dir), "-o", str(project_dir),
           "-a", "-k", "/nonexistent"] + list(extra)
    return subprocess.run(cmd, capture_output=True, text=True)


class TestCliE2E(unittest.TestCase):
    def _project(self, tmp, with_dep=True, fresh=True):
        proj = Path(tmp) / "proj.uvprojx"
        proj.write_text(Path(FIX).read_text(encoding="utf-8"), encoding="utf-8")
        if with_dep:
            objs = Path(tmp) / "Objects"
            objs.mkdir()
            dep = objs / "proj_App.dep"
            dep.write_text(DEP_BODY, encoding="utf-8")
            t = time.time() + (10 if fresh else -100)
            os.utime(str(dep), (t, t))
        return proj

    def test_fresh_dep_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._project(tmp, with_dep=True, fresh=True)
            r = run(tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            cc = json.loads((Path(tmp) / "compile_commands.json").read_text())
            self.assertTrue(any("-imacros" in e["arguments"] for e in cc))

    def test_stale_dep_warns_and_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._project(tmp, with_dep=True, fresh=False)
            r = run(tmp)
            self.assertIn("stale", (r.stdout + r.stderr).lower())
            cc = json.loads((Path(tmp) / "compile_commands.json").read_text())
            self.assertFalse(any("-imacros" in e["arguments"] for e in cc))

    def test_no_dep_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._project(tmp, with_dep=True, fresh=True)
            r = run(tmp, "--no-dep")
            cc = json.loads((Path(tmp) / "compile_commands.json").read_text())
            self.assertFalse(any("-imacros" in e["arguments"] for e in cc))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

Run: `cd scripts && python3 -m unittest tests.test_cli_e2e -v`
Expected: FAIL（`test_fresh_dep_used` 断言 `-imacros` 不成立，因 main 尚未接线）

- [ ] **Step 3: 实现**

在 `main()` 的 argparse 段追加两个参数（放在 `-o/--output` 之后）：

```python
    ap.add_argument('--no-dep', action='store_true',
                    help='Ignore Keil .dep build output; use .uvprojx only')
    ap.add_argument('--dep-path', default=None,
                    help='Explicit path to the target .dep file')
```

在 `check_macros(parser, keil)` 之后、`if args.dry_run:` 之前插入 enrichment 构造与日志：

```python
    # Build .dep enrichment (ground-truth supplement; XML stays authoritative)
    enrichment = DepEnrichment(found=False)
    if not args.no_dep:
        enrichment = DepParser(parser, dep_path_override=args.dep_path).parse()
        if enrichment.found and not enrichment.stale:
            print(f".dep: using {enrichment.dep_path} "
                  f"(+{len(enrichment.system_includes)} sysinc, "
                  f"+{len(enrichment.preinclude_files)} preinclude, "
                  f"{len(enrichment.source_files)} files)")
        elif enrichment.stale:
            print(f".dep: STALE ({enrichment.dep_path} older than .uvprojx) — "
                  f"ignored; rebuild the project to refresh system headers/preincludes.")
        else:
            print(".dep: not found — using .uvprojx only (no build output).")
    else:
        print(".dep: skipped (--no-dep).")
```

把两个 generator 的构造传入 `enrichment=enrichment`：

```python
    if not args.no_clangd:
        gen = ClangdGenerator(parser, keil,
                              use_absolute=args.absolute,
                              base_dir=output_dir,
                              enrichment=enrichment)
        gen.write(output_dir)

    if not args.no_compile_commands:
        gen = CompileCommandsGenerator(parser, keil,
                                       use_absolute=args.absolute,
                                       base_dir=output_dir,
                                       enrichment=enrichment)
        gen.write(output_dir)
```

- [ ] **Step 4: 运行全部测试确认通过**

Run: `cd scripts && python3 -m unittest discover -s tests -v`
Expected: PASS（全部：Task1–6 共 20 tests）

- [ ] **Step 5: 提交**

```bash
cd /Users/xu/MyDocuments/my-repos/keil2clangd
git add scripts/Keil2Clangd.py scripts/tests/test_cli_e2e.py
git commit -m "feat: wire .dep enrichment into main() with --no-dep/--dep-path

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: 文档更新（SKILL.md + README + 致谢）

**Files:**
- Modify: `skills/keil2clangd/SKILL.md`
- Modify: `README.md`

**Interfaces:** 无代码接口；文档需与 Task 1–6 的实际行为一致（`--no-dep`/`--dep-path`、双来源、过期护栏）。

- [ ] **Step 1: 更新 SKILL.md**

在 `## Script options` 的选项清单里补两行：

```
--no-dep                Ignore Keil .dep; use .uvprojx (XML) only
--dep-path PATH         Explicit path to the target .dep file
```

在 `### 2. Run the script` 之后新增一节：

```markdown
### 2b. Understand the two data sources (.uvprojx vs .dep)

- **`.uvprojx` (XML)** is the live source of truth for **macros (`-D`) and project include paths (`-I`)** — edit a macro in Keil and it takes effect immediately, no build needed.
- **`.dep`** is generated by Keil **after a build** and supplies only what XML can't: compiler **system headers**, **preinclude** headers (`-imacros`), and the **real compiled file list**. The script uses it automatically when fresh.
- **Staleness guard:** if `.uvprojx` is newer than `.dep`, the script prints `.dep: STALE ... ignored` and falls back to XML-only. If you changed macros, that's expected — rebuild the project (or ignore, since macros already come from XML).
- Read the script's `.dep:` log line to know which source was used. Use `--no-dep` to force XML-only, `--dep-path` for a non-standard output dir.
```

在校验步骤（`### 4. Validate include paths`）里补一句：把 `.dep` 带来的系统头/preinclude 路径也纳入存在性校验。

- [ ] **Step 2: 更新 README.md**

在描述段补一句「优先读 `.uvprojx` 免编译生成；若工程已编译，自动吸收 `.dep` 的系统头/预包含/真实文件清单，并有过期检测」。在「## 致谢」补一句：`.dep` 解析思路参考 [vankubo/uvConvertor](https://github.com/vankubo/uvConvertor) 与 [a3750/uvconvertor](https://github.com/a3750/uvconvertor)。

- [ ] **Step 3: 人工核对**

Run: `cd scripts && python3 Keil2Clangd.py -h`
Expected: 帮助里出现 `--no-dep` 与 `--dep-path`，与 SKILL.md 描述一致。

- [ ] **Step 4: 提交**

```bash
cd /Users/xu/MyDocuments/my-repos/keil2clangd
git add skills/keil2clangd/SKILL.md README.md
git commit -m "docs: document .dep two-source model, staleness guard, new flags

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review 记录

- **Spec 覆盖**：§2 hybrid 表 → Task 4/5（宏来自 XML、系统头/preinclude/文件清单来自 dep）；§2 过期护栏 → Task 3 + Task 6 日志；§3.1 DepParser → Task 1+3；§3.2/3.3 生成器 → Task 4/5；§3.4 CLI → Task 6；§3.5 SKILL → Task 7；§5 降级 → Task 3（try/except + missing/stale）；§6 测试策略 → 各任务 Step1 + Task6 discover；§7 不做项（IAR/覆盖宏/--pattern/vsix）均未进任务；§8 致谢 → Task 7。无遗漏。
- **占位符扫描**：无 TBD/TODO；每个代码步骤含完整代码。
- **类型一致性**：`DepEnrichment` 字段（found/stale/dep_path/system_includes/preinclude_files/source_files）在 Task 1 定义，Task 3/4/5/6 使用一致；`enrichment=` 关键字参数在 Task 4/5 定义、Task 6 传入一致；`_parse_dep_text` 返回键名（system_includes/preinclude_files/source_files）Task 1↔3 一致。
