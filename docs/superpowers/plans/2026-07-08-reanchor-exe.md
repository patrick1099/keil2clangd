# Re-anchor Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A surgical re-anchor tool (`scripts/ReAnchor.py`, frozen to `keil2clangd-reanchor.exe`) that fixes machine/location-bound paths in `.clangd` + `compile_commands.json` after a project move, never touching AI-augmented content.

**Architecture:** Pure functions for path decisions and per-file surgery (line-level for `.clangd`, JSON-level for `compile_commands.json`), a thin `main()` that does scan-pass → lazy Keil probe (reusing `KeilPathResolver` via import) → apply-pass. PyInstaller onefile for the exe.

**Tech Stack:** Python 3 stdlib only at runtime (`argparse/json/re/shutil/pathlib`); `unittest` for tests; PyInstaller (build-time only).

**Spec:** `docs/superpowers/specs/2026-07-08-reanchor-exe-design.md` — read it before starting.

## Global Constraints

- Repo: `C:\Users\huawei\.claude\plugins-dev\keil2clangd`, branch `feat/reanchor-exe`, git identity must be `patrick1099 <hsheng416@gmail.com>` (verify with `git config user.email` before first commit).
- Runtime code: **stdlib only**, no third-party imports in `ReAnchor.py`.
- Run Python as `py -3` (Windows; avoids WindowsApps alias).
- Core invariant: **bytes not belonging to a replaced path must survive unchanged** (comments, AI-added `-D` lines, ordering, indentation).
- `.clangd` I/O: `open(..., encoding='utf-8', newline='')` both directions (preserve CRLF/LF exactly).
- `compile_commands.json` write: `json.dump(entries, f, indent=4, ensure_ascii=False)` — same as the existing generator.
- Tests: stdlib `unittest`, live in `scripts/tests/`, run via `cd scripts && py -3 -m unittest discover -s tests -v`. Follow the import style of the existing test files (read `scripts/tests/test_dep_parser.py` first if unsure).
- Commit messages end with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

---

### Task 1: Path-decision primitives

**Files:**
- Create: `scripts/ReAnchor.py`
- Test: `scripts/tests/test_reanchor_paths.py`

**Interfaces:**
- Consumes: `from Keil2Clangd import KeilPathResolver, _dedup` (both already exist in `scripts/Keil2Clangd.py`).
- Produces (used by Tasks 2–4):
  - `_is_windows_abs(s: str) -> bool`
  - `remap_dead_path(path_str: str, keil_root) -> Optional[str]` — `keil_root` is `Path | str | None`
  - `fix_flag_value(path_str: str, keil_root) -> tuple` — returns `(None, None)` untouched, `(new_path, 'fixed')`, or `(None, 'dead')`

- [ ] **Step 1: Write the failing test**

```python
# scripts/tests/test_reanchor_paths.py
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ReAnchor import _is_windows_abs, remap_dead_path, fix_flag_value


class TestIsWindowsAbs(unittest.TestCase):
    def test_drive_paths_are_absolute(self):
        self.assertTrue(_is_windows_abs("C:/Keil_v5/ARM"))
        self.assertTrue(_is_windows_abs("d:\\Keil_v5"))

    def test_relative_paths_are_not(self):
        self.assertFalse(_is_windows_abs("App/Code"))
        self.assertFalse(_is_windows_abs("../up"))
        self.assertFalse(_is_windows_abs("-IApp"))


class TestRemapDeadPath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.keil = Path(self.tmp.name) / "Keil_v5"
        (self.keil / "ARM" / "ARMCLANG" / "include").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_remaps_via_arm_suffix_when_target_exists(self):
        got = remap_dead_path("D:/OldKeil/ARM/ARMCLANG/include", self.keil)
        self.assertEqual(got, str(self.keil / "ARM/ARMCLANG/include").replace("\\", "/"))

    def test_none_when_suffix_missing_under_new_root(self):
        self.assertIsNone(remap_dead_path("D:/OldKeil/ARM/PACK/ARM/CMSIS/9.9.9/x", self.keil))

    def test_none_without_arm_marker_or_keil_root(self):
        self.assertIsNone(remap_dead_path("D:/Other/include", self.keil))
        self.assertIsNone(remap_dead_path("D:/OldKeil/ARM/ARMCLANG/include", None))


class TestFixFlagValue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.keil = Path(self.tmp.name) / "Keil_v5"
        (self.keil / "ARM" / "ARMCLANG" / "include").mkdir(parents=True)
        self.alive = Path(self.tmp.name) / "alive"
        self.alive.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_relative_never_touched(self):
        self.assertEqual(fix_flag_value("App/Code", self.keil), (None, None))

    def test_alive_absolute_never_touched(self):
        p = str(self.alive).replace("\\", "/")
        self.assertEqual(fix_flag_value(p, self.keil), (None, None))

    def test_dead_and_remappable_is_fixed(self):
        new, status = fix_flag_value("D:/OldKeil/ARM/ARMCLANG/include", self.keil)
        self.assertEqual(status, "fixed")
        self.assertTrue(new.endswith("ARM/ARMCLANG/include"))

    def test_dead_and_unmappable_is_dead(self):
        self.assertEqual(fix_flag_value("D:/Gone/NoArm/include", self.keil), (None, "dead"))
        self.assertEqual(fix_flag_value("D:/OldKeil/ARM/ARMCLANG/include", None), (None, "dead"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:\Users\huawei\.claude\plugins-dev\keil2clangd\scripts && py -3 -m unittest tests.test_reanchor_paths -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ReAnchor'`

- [ ] **Step 3: Write minimal implementation**

```python
#!/usr/bin/env python3
"""ReAnchor - surgically re-anchor .clangd / compile_commands.json after a project move.

Rewrites only machine/location-bound paths:
  * compile_commands.json "directory" -> current project root (clangd requires absolute)
  * dead absolute toolchain -I / -imacros -> re-probed Keil location
Everything else (relative -I, -D macros, comments, AI-added lines) survives byte-for-byte.
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from Keil2Clangd import KeilPathResolver, _dedup  # noqa: E402

_WIN_ABS_RE = re.compile(r'^[A-Za-z]:[/\\]')
_ARM_MARKER = '/ARM/'


def _is_windows_abs(s):
    return bool(_WIN_ABS_RE.match(s))


def remap_dead_path(path_str, keil_root):
    """Map a dead toolchain path onto keil_root via its /ARM/... suffix.

    Returns the forward-slashed new path, or None when keil_root is unknown,
    the path has no /ARM/ segment, or the suffix does not exist under keil_root.
    """
    if keil_root is None:
        return None
    norm = path_str.replace('\\', '/')
    idx = norm.upper().find(_ARM_MARKER)
    if idx < 0:
        return None
    cand = Path(keil_root) / norm[idx + 1:]
    if cand.exists():
        return str(cand).replace('\\', '/')
    return None


def fix_flag_value(path_str, keil_root):
    """Decide what to do with one -I/-imacros path value.

    Returns (new_path, status):
      (None, None)   -- relative or still alive: never touch
      (new, 'fixed') -- dead, remapped onto keil_root
      (None, 'dead') -- dead and not fixable: keep + warn
    """
    if not _is_windows_abs(path_str):
        return None, None
    if Path(path_str).exists():
        return None, None
    new = remap_dead_path(path_str, keil_root)
    if new:
        return new, 'fixed'
    return None, 'dead'
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd C:\Users\huawei\.claude\plugins-dev\keil2clangd\scripts && py -3 -m unittest tests.test_reanchor_paths -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
cd /c/Users/huawei/.claude/plugins-dev/keil2clangd
git add scripts/ReAnchor.py scripts/tests/test_reanchor_paths.py
git commit -m "feat: ReAnchor path-decision primitives

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: `.clangd` line surgery

**Files:**
- Modify: `scripts/ReAnchor.py` (append after `fix_flag_value`)
- Test: `scripts/tests/test_reanchor_clangd.py`

**Interfaces:**
- Consumes: `fix_flag_value(path_str, keil_root)` from Task 1.
- Produces: `reanchor_clangd_text(text: str, keil_root) -> (new_text: str, changes: list[tuple[str, str]], dead: list[str])`. `changes` are `(old, new)` pairs; `dead` are kept-but-broken paths.

- [ ] **Step 1: Write the failing test**

```python
# scripts/tests/test_reanchor_clangd.py
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ReAnchor import reanchor_clangd_text

SAMPLE = """CompileFlags:
  Add:
    # Keil project macros
    - -D__DEBUG
    # AI-added ARMCC compat macro (must survive)
    - -D__weak=__attribute__((weak))
    # Include paths
    - -IApp/Code
    - -ID:/OldKeil/ARM/ARMCLANG/include
    # Preinclude headers (from .dep)
    - -imacros
    - D:/OldKeil/ARM/ARMCLANG/include/pre.h
  Remove:
    - -W*
"""


class TestReanchorClangd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.keil = Path(self.tmp.name) / "Keil_v5"
        inc = self.keil / "ARM" / "ARMCLANG" / "include"
        inc.mkdir(parents=True)
        (inc / "pre.h").write_text("", encoding="utf-8")
        self.new_inc = str(inc).replace("\\", "/")

    def tearDown(self):
        self.tmp.cleanup()

    def test_dead_I_and_imacros_fixed_others_untouched(self):
        new_text, changes, dead = reanchor_clangd_text(SAMPLE, self.keil)
        self.assertIn(f"    - -I{self.new_inc}\n", new_text)
        self.assertIn(f"    - {self.new_inc}/pre.h\n", new_text)
        self.assertEqual(len(changes), 2)
        self.assertEqual(dead, [])
        # non-path lines byte-identical
        for line in SAMPLE.split("\n"):
            if "OldKeil" not in line:
                self.assertIn(line + ("\n" if line else ""), new_text + "\n")

    def test_ai_added_lines_and_comments_survive(self):
        new_text, _, _ = reanchor_clangd_text(SAMPLE, self.keil)
        self.assertIn("- -D__weak=__attribute__((weak))", new_text)
        self.assertIn("# AI-added ARMCC compat macro (must survive)", new_text)
        self.assertIn("- -IApp/Code", new_text)

    def test_no_keil_found_keeps_text_reports_dead(self):
        new_text, changes, dead = reanchor_clangd_text(SAMPLE, None)
        self.assertEqual(new_text, SAMPLE)
        self.assertEqual(changes, [])
        self.assertEqual(sorted(dead), sorted([
            "D:/OldKeil/ARM/ARMCLANG/include",
            "D:/OldKeil/ARM/ARMCLANG/include/pre.h",
        ]))

    def test_idempotent_when_paths_alive(self):
        fixed, _, _ = reanchor_clangd_text(SAMPLE, self.keil)
        again, changes, dead = reanchor_clangd_text(fixed, self.keil)
        self.assertEqual(again, fixed)
        self.assertEqual(changes, [])
        self.assertEqual(dead, [])

    def test_crlf_preserved(self):
        crlf = SAMPLE.replace("\n", "\r\n")
        new_text, changes, _ = reanchor_clangd_text(crlf, self.keil)
        self.assertEqual(len(changes), 2)
        self.assertNotIn("\n    - -D__DEBUG\n", new_text.replace("\r\n", "\x00"))
        self.assertIn("\r\n", new_text)
        self.assertEqual(new_text.count("\r\n"), crlf.count("\r\n"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:\Users\huawei\.claude\plugins-dev\keil2clangd\scripts && py -3 -m unittest tests.test_reanchor_clangd -v`
Expected: FAIL — `ImportError: cannot import name 'reanchor_clangd_text'`

- [ ] **Step 3: Write minimal implementation** (append to `scripts/ReAnchor.py`)

```python
_CLANGD_I_RE = re.compile(r'^(\s*-\s+-I)(.*?)(\s*)$')
_CLANGD_BARE_RE = re.compile(r'^(\s*-\s+)([^-#\s].*?)(\s*)$')


def reanchor_clangd_text(text, keil_root):
    """Line-level surgery on .clangd text. Returns (new_text, changes, dead).

    Only -I values and the value line following '- -imacros' are candidates;
    every other line is passed through untouched. CRLF endings survive because
    the trailing-whitespace group captures the \r.
    """
    lines = text.split('\n')
    changes = []
    dead = []
    expect_imacros_value = False
    for i, line in enumerate(lines):
        m = _CLANGD_I_RE.match(line)
        if m:
            expect_imacros_value = False
        elif expect_imacros_value:
            m = _CLANGD_BARE_RE.match(line)
            expect_imacros_value = False
            if not m:
                continue
        else:
            if line.strip() == '- -imacros':
                expect_imacros_value = True
            continue
        val = m.group(2)
        new, status = fix_flag_value(val, keil_root)
        if status == 'fixed':
            lines[i] = m.group(1) + new + m.group(3)
            changes.append((val, new))
        elif status == 'dead':
            dead.append(val)
    return '\n'.join(lines), changes, dead
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd C:\Users\huawei\.claude\plugins-dev\keil2clangd\scripts && py -3 -m unittest tests.test_reanchor_clangd -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
cd /c/Users/huawei/.claude/plugins-dev/keil2clangd
git add scripts/ReAnchor.py scripts/tests/test_reanchor_clangd.py
git commit -m "feat: .clangd line surgery (reanchor_clangd_text)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: compile_commands.json surgery

**Files:**
- Modify: `scripts/ReAnchor.py` (append after `reanchor_clangd_text`)
- Test: `scripts/tests/test_reanchor_cc.py`

**Interfaces:**
- Consumes: `fix_flag_value` (Task 1).
- Produces: `reanchor_entries(entries: list, new_root: str, keil_root) -> (changes: list[tuple], dead: list[str])`. **Mutates `entries` in place.** `new_root` is the forward-slashed absolute project root. Rebuilds `entry['command']` as `' '.join(arguments)` **only when an argument changed** (generator emits `command == ' '.join(arguments)`, so this is format-preserving; a directory-only change never touches `command`).

- [ ] **Step 1: Write the failing test**

```python
# scripts/tests/test_reanchor_cc.py
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ReAnchor import reanchor_entries

NEW_ROOT = "C:/NewPlace/Code"


def make_entry(dead_inc="D:/OldKeil/ARM/ARMCLANG/include"):
    args = ["arm-none-eabi-gcc", "-c", "App/main.c", "-D__DEBUG",
            "-IApp/Code", f"-I{dead_inc}",
            "-imacros", f"{dead_inc}/pre.h"]
    return {
        "command": " ".join(args),
        "arguments": list(args),
        "directory": "C:/Users/dell/Old/Code",
        "file": "App/main.c",
    }


class TestReanchorEntries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.keil = Path(self.tmp.name) / "Keil_v5"
        inc = self.keil / "ARM" / "ARMCLANG" / "include"
        inc.mkdir(parents=True)
        (inc / "pre.h").write_text("", encoding="utf-8")
        self.new_inc = str(inc).replace("\\", "/")

    def tearDown(self):
        self.tmp.cleanup()

    def test_directory_rewritten_everywhere(self):
        entries = [make_entry(), make_entry()]
        reanchor_entries(entries, NEW_ROOT, self.keil)
        self.assertTrue(all(e["directory"] == NEW_ROOT for e in entries))

    def test_dead_toolchain_args_fixed_and_command_synced(self):
        entries = [make_entry()]
        reanchor_entries(entries, NEW_ROOT, self.keil)
        args = entries[0]["arguments"]
        self.assertIn(f"-I{self.new_inc}", args)
        self.assertIn(f"{self.new_inc}/pre.h", args)
        self.assertEqual(entries[0]["command"], " ".join(args))

    def test_relative_and_defines_untouched(self):
        entries = [make_entry()]
        reanchor_entries(entries, NEW_ROOT, self.keil)
        args = entries[0]["arguments"]
        self.assertIn("-IApp/Code", args)
        self.assertIn("-D__DEBUG", args)
        self.assertEqual(entries[0]["file"], "App/main.c")

    def test_command_untouched_when_only_directory_changes(self):
        alive = str(self.keil / "ARM" / "ARMCLANG" / "include").replace("\\", "/")
        entry = make_entry(dead_inc=alive)  # all -I alive
        original_command = "HAND-EDITED " + entry["command"]
        entry["command"] = original_command
        changes, dead = reanchor_entries([entry], NEW_ROOT, self.keil)
        self.assertEqual(entry["command"], original_command)
        self.assertEqual(entry["directory"], NEW_ROOT)
        self.assertEqual(dead, [])

    def test_idempotent(self):
        entries = [make_entry()]
        reanchor_entries(entries, NEW_ROOT, self.keil)
        changes, dead = reanchor_entries(entries, NEW_ROOT, self.keil)
        self.assertEqual(changes, [])
        self.assertEqual(dead, [])

    def test_unmappable_dead_reported(self):
        entries = [make_entry()]
        changes, dead = reanchor_entries(entries, NEW_ROOT, None)
        self.assertIn("D:/OldKeil/ARM/ARMCLANG/include", dead)
        self.assertEqual([c for c in changes if c[0] != "C:/Users/dell/Old/Code"], [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:\Users\huawei\.claude\plugins-dev\keil2clangd\scripts && py -3 -m unittest tests.test_reanchor_cc -v`
Expected: FAIL — `ImportError: cannot import name 'reanchor_entries'`

- [ ] **Step 3: Write minimal implementation** (append to `scripts/ReAnchor.py`)

```python
def reanchor_entries(entries, new_root, keil_root):
    """Mutate compile-command entries in place. Returns (changes, dead).

    Rewrites 'directory' to new_root and fixes dead toolchain -I/-imacros in
    'arguments'. 'command' is rebuilt as ' '.join(arguments) only when an
    argument actually changed, so hand-edited commands survive a pure
    directory re-anchor.
    """
    changes = []
    dead = []
    for entry in entries:
        args_changed = False
        old_dir = entry.get('directory')
        if old_dir != new_root:
            entry['directory'] = new_root
            changes.append((old_dir, new_root))
        args = entry.get('arguments')
        if not args:
            continue
        i = 0
        while i < len(args):
            a = args[i]
            val = prefix = None
            if a.startswith('-I'):
                val, prefix, at = a[2:], '-I', i
            elif a == '-imacros' and i + 1 < len(args):
                i += 1
                val, prefix, at = args[i], '', i
            if val is not None:
                new, status = fix_flag_value(val, keil_root)
                if status == 'fixed':
                    args[at] = prefix + new
                    changes.append((val, new))
                    args_changed = True
                elif status == 'dead':
                    dead.append(val)
            i += 1
        if args_changed and 'command' in entry:
            entry['command'] = ' '.join(args)
    return changes, dead
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd C:\Users\huawei\.claude\plugins-dev\keil2clangd\scripts && py -3 -m unittest tests.test_reanchor_cc -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
cd /c/Users/huawei/.claude/plugins-dev/keil2clangd
git add scripts/ReAnchor.py scripts/tests/test_reanchor_cc.py
git commit -m "feat: compile_commands.json surgery (reanchor_entries)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: CLI driver — scan pass, lazy Keil probe, backup, summary

**Files:**
- Modify: `scripts/ReAnchor.py` (append; also `if __name__` block at end)
- Test: `scripts/tests/test_reanchor_cli.py`

**Interfaces:**
- Consumes: `reanchor_clangd_text` (Task 2), `reanchor_entries` (Task 3), `KeilPathResolver` (existing; `KeilPathResolver(keil_path=X).keil_root` — with a valid `keil_path` it uses it directly and never prompts; probe cascade env→config→scan→prompt otherwise; EOFError on prompt leaves `keil_root=None`).
- Produces: `main(argv=None) -> int`. CLI: `--root PATH`, `-k/--keil-path PATH`, `--dry-run`, `--no-pause`. Exit 0 on success (warnings allowed), 1 when neither target file exists.
- Key behaviors (acceptance scenarios from the spec):
  1. No dead paths → `KeilPathResolver` is **never constructed** → zero interaction (same-machine move).
  2. Dead paths → probe once, then apply. Keil not found → fix `directory` only, keep dead lines, warn.
  3. Backups `.clangd.bak` / `compile_commands.json.bak` written only when the file actually changes and not `--dry-run`.
  4. Frozen-exe pause (`input()`) unless `--no-pause`; never pauses when run as a script (tests won't hang).

- [ ] **Step 1: Write the failing test**

```python
# scripts/tests/test_reanchor_cli.py
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
REANCHOR = SCRIPTS / "ReAnchor.py"

CLANGD = """CompileFlags:
  Add:
    # AI-added compat macro (must survive)
    - -D__weak=__attribute__((weak))
    - -IApp/Code
    - -ID:/OldKeil/ARM/ARMCLANG/include
  Remove:
    - -W*
"""


def run_cli(*argv):
    return subprocess.run([sys.executable, str(REANCHOR)] + list(argv),
                          capture_output=True, text=True, stdin=subprocess.DEVNULL)


class TestReanchorCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.proj = base / "proj"
        self.proj.mkdir()
        self.keil = base / "Keil_v5"
        (self.keil / "ARM" / "ARMCLANG" / "include").mkdir(parents=True)
        (self.proj / ".clangd").write_text(CLANGD, encoding="utf-8")
        args = ["arm-none-eabi-gcc", "-c", "App/main.c",
                "-IApp/Code", "-ID:/OldKeil/ARM/ARMCLANG/include"]
        entries = [{"command": " ".join(args), "arguments": args,
                    "directory": "C:/Users/dell/Old/Code", "file": "App/main.c"}]
        (self.proj / "compile_commands.json").write_text(
            json.dumps(entries, indent=4), encoding="utf-8")
        self.new_root = str(self.proj).replace("\\", "/")
        self.new_inc = str(self.keil / "ARM/ARMCLANG/include").replace("\\", "/")

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_reanchor_with_explicit_keil(self):
        r = run_cli("--root", str(self.proj), "--keil-path", str(self.keil))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        cc = json.loads((self.proj / "compile_commands.json").read_text(encoding="utf-8"))
        self.assertEqual(cc[0]["directory"], self.new_root)
        self.assertIn(f"-I{self.new_inc}", cc[0]["arguments"])
        text = (self.proj / ".clangd").read_text(encoding="utf-8")
        self.assertIn(f"-I{self.new_inc}", text)
        self.assertIn("- -D__weak=__attribute__((weak))", text)
        self.assertTrue((self.proj / ".clangd.bak").exists())
        self.assertTrue((self.proj / "compile_commands.json.bak").exists())

    def test_dry_run_writes_nothing(self):
        before = (self.proj / ".clangd").read_text(encoding="utf-8")
        r = run_cli("--root", str(self.proj), "--keil-path", str(self.keil), "--dry-run")
        self.assertEqual(r.returncode, 0)
        self.assertEqual((self.proj / ".clangd").read_text(encoding="utf-8"), before)
        self.assertFalse((self.proj / ".clangd.bak").exists())

    def test_no_dead_paths_zero_interaction(self):
        # make every absolute path alive: re-anchor once, then run again w/o keil-path
        run_cli("--root", str(self.proj), "--keil-path", str(self.keil))
        r = run_cli("--root", str(self.proj))  # stdin closed; would hang/EOF if probed
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("Keil", r.stdout.replace("OldKeil", ""))

    def test_unmappable_dead_path_kept_with_warning(self):
        # Suffix that exists under NO Keil install (even the real machine one),
        # so no matter what the probe finds, this path stays dead -> kept + warn.
        dead = "D:/OldKeil/ARM/NOSUCH_XYZ/include"
        args = ["arm-none-eabi-gcc", "-c", "App/main.c", f"-I{dead}"]
        entries = [{"command": " ".join(args), "arguments": args,
                    "directory": "C:/Users/dell/Old/Code", "file": "App/main.c"}]
        (self.proj / "compile_commands.json").write_text(
            json.dumps(entries, indent=4), encoding="utf-8")
        r = run_cli("--root", str(self.proj), "--keil-path", str(self.keil))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        cc = json.loads((self.proj / "compile_commands.json").read_text(encoding="utf-8"))
        self.assertEqual(cc[0]["directory"], self.new_root)   # directory still fixed
        self.assertIn(f"-I{dead}", cc[0]["arguments"])        # dead -I kept
        self.assertIn("WARNING", r.stdout)

    def test_missing_both_files_errors(self):
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        r = run_cli("--root", str(empty))
        self.assertEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main()
```

Design note baked into the tests above: never assert "Keil not found" behavior by passing a bad `--keil-path` alone — on this dev machine the resolver's fallback scan finds the real `C:/Keil_v5` and would happily remap `ARM/ARMCLANG/include`. That is why `test_unmappable_dead_path_kept_with_warning` uses a suffix (`ARM/NOSUCH_XYZ/include`) that exists under no Keil install.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:\Users\huawei\.claude\plugins-dev\keil2clangd\scripts && py -3 -m unittest tests.test_reanchor_cli -v`
Expected: FAIL — CLI exits with argparse/AttributeError (no `main`), all tests error.

- [ ] **Step 3: Write minimal implementation** (append to `scripts/ReAnchor.py`)

```python
def _default_root():
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def _backup(path):
    shutil.copy2(str(path), str(path) + '.bak')


def _report(name, changes, dead, dry_run):
    tag = 'would rewrite' if dry_run else 'rewrote'
    for old, new in _dedup([tuple(c) for c in changes]):
        print("{0}: {1} {2} -> {3}".format(name, tag, old, new))
    for p in _dedup(dead):
        print("{0}: WARNING kept dead path {1} "
              "(not found under new Keil; re-run the generator/skill)".format(name, p))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Re-anchor .clangd / compile_commands.json after moving a project")
    ap.add_argument('--root', default=None,
                    help='Project root holding the files (default: exe dir / cwd)')
    ap.add_argument('-k', '--keil-path', default=None,
                    help='Keil installation path (skips auto-probe)')
    ap.add_argument('--dry-run', action='store_true',
                    help='Report changes without writing files')
    ap.add_argument('--no-pause', action='store_true',
                    help='Do not wait for Enter before exiting (frozen exe)')
    args = ap.parse_args(argv)

    root = Path(args.root).resolve() if args.root else _default_root()
    new_root = str(root).replace('\\', '/')
    clangd_path = root / '.clangd'
    cc_path = root / 'compile_commands.json'

    if not clangd_path.is_file() and not cc_path.is_file():
        print("ERROR: neither .clangd nor compile_commands.json found in " + new_root)
        return _finish(1, args)

    clangd_text = None
    entries = None
    dead_found = []
    if clangd_path.is_file():
        with open(str(clangd_path), 'r', encoding='utf-8', newline='') as f:
            clangd_text = f.read()
        _, _, d = reanchor_clangd_text(clangd_text, None)   # scan: keil_root=None
        dead_found += d
    if cc_path.is_file():
        with open(str(cc_path), 'r', encoding='utf-8') as f:
            entries = json.load(f)
        import copy
        _, d = reanchor_entries(copy.deepcopy(entries), new_root, None)  # scan
        dead_found += d

    keil_root = None
    if dead_found:
        print("Dead toolchain paths detected:")
        for p in _dedup(dead_found):
            print("  " + p)
        keil_root = KeilPathResolver(keil_path=args.keil_path).keil_root
        if keil_root is None:
            print("WARNING: Keil installation not found -- "
                  "dead toolchain paths will be kept as-is.")

    total = 0
    if clangd_text is not None:
        new_text, changes, dead = reanchor_clangd_text(clangd_text, keil_root)
        if new_text != clangd_text and not args.dry_run:
            _backup(clangd_path)
            with open(str(clangd_path), 'w', encoding='utf-8', newline='') as f:
                f.write(new_text)
        _report('.clangd', changes, dead, args.dry_run)
        total += len(_dedup([tuple(c) for c in changes]))
    if entries is not None:
        changes, dead = reanchor_entries(entries, new_root, keil_root)
        if changes and not args.dry_run:
            _backup(cc_path)
            with open(str(cc_path), 'w', encoding='utf-8') as f:
                json.dump(entries, f, indent=4, ensure_ascii=False)
        _report('compile_commands.json', changes, dead, args.dry_run)
        total += len(_dedup([tuple(c) for c in changes]))

    print("\n{0}: {1} path(s).".format(
        'Would change' if args.dry_run else 'Changed', total))
    return _finish(0, args)


def _finish(rc, args):
    if getattr(sys, 'frozen', False) and not args.no_pause:
        try:
            input("\nPress Enter to exit...")
        except EOFError:
            pass
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd C:\Users\huawei\.claude\plugins-dev\keil2clangd\scripts && py -3 -m unittest tests.test_reanchor_cli -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Run the FULL suite (regression)**

Run: `cd C:\Users\huawei\.claude\plugins-dev\keil2clangd\scripts && py -3 -m unittest discover -s tests -v`
Expected: PASS — 21 pre-existing + 25 new = 46 tests, OK

- [ ] **Step 6: Commit**

```bash
cd /c/Users/huawei/.claude/plugins-dev/keil2clangd
git add scripts/ReAnchor.py scripts/tests/test_reanchor_cli.py
git commit -m "feat: ReAnchor CLI driver with lazy Keil probe and backups

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: PyInstaller build script + exe smoke test

**Files:**
- Create: `scripts/build_exe.bat`
- Modify: `.gitignore` (append build artifacts)

**Interfaces:**
- Consumes: `scripts/ReAnchor.py` (complete after Task 4).
- Produces: `scripts/dist/keil2clangd-reanchor.exe` (NOT committed; local artifact / GitHub Release).

- [ ] **Step 1: Write the build script**

```bat
@echo off
REM Build keil2clangd-reanchor.exe (PyInstaller onefile).
REM Output: dist\keil2clangd-reanchor.exe  -- copy it next to your project's .clangd.
cd /d "%~dp0"
py -3 -m PyInstaller --version >nul 2>nul || py -3 -m pip install pyinstaller
py -3 -m PyInstaller --onefile --console --name keil2clangd-reanchor ReAnchor.py
echo.
echo Done: %~dp0dist\keil2clangd-reanchor.exe
```

- [ ] **Step 2: Append to `.gitignore`** (create section if absent)

```gitignore
# PyInstaller build artifacts
scripts/build/
scripts/dist/
scripts/*.spec
```

- [ ] **Step 3: Build**

Run: `cmd /c "C:\Users\huawei\.claude\plugins-dev\keil2clangd\scripts\build_exe.bat"`
Expected: ends with `Done: ...dist\keil2clangd-reanchor.exe`; file exists.

- [ ] **Step 4: Smoke-test the exe on a scratch fixture**

```powershell
$fix = Join-Path $env:TEMP "reanchor-smoke"; Remove-Item -Recurse -Force $fix -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force "$fix\Keil_v5\ARM\ARMCLANG\include" | Out-Null
Set-Content "$fix\.clangd" "CompileFlags:`n  Add:`n    - -ID:/OldKeil/ARM/ARMCLANG/include`n" -Encoding utf8
& "C:\Users\huawei\.claude\plugins-dev\keil2clangd\scripts\dist\keil2clangd-reanchor.exe" --root $fix --keil-path "$fix\Keil_v5" --no-pause
Get-Content "$fix\.clangd"
```
Expected: exit 0; `.clangd` now contains `-I<fix>/Keil_v5/ARM/ARMCLANG/include`; `.clangd.bak` exists. (This also proves the frozen exe bundles `Keil2Clangd` correctly.)

- [ ] **Step 5: Verify git status is clean of artifacts, then commit**

Run: `cd /c/Users/huawei/.claude/plugins-dev/keil2clangd && git status --porcelain`
Expected: only `scripts/build_exe.bat` and `.gitignore` listed (no dist/build/spec).

```bash
git add scripts/build_exe.bat .gitignore
git commit -m "build: PyInstaller onefile script for keil2clangd-reanchor.exe

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Documentation (SKILL.md + README)

**Files:**
- Modify: `skills/keil2clangd/SKILL.md` (add section after "### 7. Report and restart", before "## Common issues the script can't handle")
- Modify: `README.md` (add short feature bullet/section; read the file first and match its existing structure/tone)

**Interfaces:** none (docs only). Content below is the exact SKILL.md insertion.

- [ ] **Step 1: Insert into SKILL.md**

```markdown
## Project moved / new machine (re-anchor)

Generated files contain machine/location-bound paths. Measured behavior (clangd 22, Windows):

1. `compile_commands.json`'s `directory` MUST be a correct absolute path on the
   current machine — a relative value never works (clangd hard limit), and a stale
   absolute value only works while clangd's CWD happens to be the project root.
2. Relative `-I` in `.clangd` resolve against that `directory` anchor.
3. Absolute toolchain `-I` (e.g. `C:/Keil_v5/...`) break across machines
   (different drive/version) — only re-probing can fix them.

Run the re-anchor tool after moving the project (same machine) or copying it to
another machine:

```powershell
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/ReAnchor.py" --root <project_root>
# or double-click keil2clangd-reanchor.exe placed next to .clangd (build:
# scripts/build_exe.bat; no Python/plugin needed on the target machine)
```

Behavior:
- Same machine, moved folder: fully automatic — rewrites `directory`, leaves
  everything else untouched.
- New machine: probes Keil (`KEIL_PATH` -> `~/.keil2clangd.json` -> common
  locations -> interactive prompt, answer saved) and rewrites dead toolchain
  `-I`/`-imacros`. Pack-version mismatches are kept + warned — re-run this
  skill to regenerate those entries.
- Surgical by design: relative `-I`, `-D` macros, comments and AI-added lines
  survive byte-for-byte. Originals backed up to `*.bak`. `--dry-run` previews.

Flags: `--root PATH`, `-k/--keil-path PATH`, `--dry-run`, `--no-pause`.
```

- [ ] **Step 2: Update README.md**

Read `README.md`, add a matching-style short section/bullet: what `ReAnchor.py`/the exe does (one paragraph), how to build (`scripts/build_exe.bat`), and the two scenarios. Keep it under ~15 lines; link to SKILL.md for detail.

- [ ] **Step 3: Commit**

```bash
cd /c/Users/huawei/.claude/plugins-dev/keil2clangd
git add skills/keil2clangd/SKILL.md README.md
git commit -m "docs: re-anchor section (clangd path-resolution findings + usage)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Final verification

**Files:** none new.

- [ ] **Step 1: Full test suite**

Run: `cd C:\Users\huawei\.claude\plugins-dev\keil2clangd\scripts && py -3 -m unittest discover -s tests -v`
Expected: all tests OK (46).

- [ ] **Step 2: Real-world dry-run (read-only check against the actual firmware project)**

Run: `py -3 C:\Users\huawei\.claude\plugins-dev\keil2clangd\scripts\ReAnchor.py --root C:\Users\huawei\Desktop\MyProjects\20260525-xinao\Code --dry-run`
Expected: exit 0, `Would change: 0 path(s).` (that project was just fixed — proves idempotence on real data). No files modified.

- [ ] **Step 3: Confirm branch state**

Run: `cd /c/Users/huawei/.claude/plugins-dev/keil2clangd && git log --oneline main..HEAD && git status --porcelain`
Expected: spec + plan + ~6 implementation commits; clean tree.

Then hand off to superpowers:finishing-a-development-branch (push + PR).
