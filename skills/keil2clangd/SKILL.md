---
name: keil2clangd
description: Generate and validate .clangd + compile_commands.json for embedded C projects from Keil .uvprojx, IAR .ewp (any architecture — ICCARM, ICCRL78, ICCRX, ICC430), or CMake. Use when setting up clangd-based jump/completion/diagnostics for a firmware project, when clangd reports missing macros / include paths / vendor-extension syntax errors, or when cross-file jump-to-definition silently fails while same-file navigation works.
---

# Project to clangd Configuration Generator

Generate `.clangd` and `compile_commands.json` for an embedded C project, then
validate the output and fix what the scripts cannot.

## Pick a backend

```powershell
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/Proj2Clangd.py" -p <dir> --detect-only
```

`Proj2Clangd.py` detects the project type and forwards every other argument to
the matching backend. The backends also run standalone:

| Project | Detected by | Backend | What it does |
|---|---|---|---|
| Keil MDK | `*.uvprojx` | `Keil2Clangd.py` | Parses XML + `.dep`, synthesises all flags |
| IAR EW | `*.ewp` | `Iar2Clangd.py` | Parses XML, **probes the real IAR compiler** for macros |
| CMake | `CMakeLists.txt` | `Cmake2Clangd.py` | Runs configure, then makes CMake's own database discoverable |

If a tree holds more than one, `Proj2Clangd.py` refuses to guess — pass
`--kind keil|iar|cmake`.

---

# Keil (.uvprojx)

## Keil path configuration

The script persists the Keil installation path to `~/.keil2clangd.json`.
Discovery priority: `-k`/`--keil-path` → `KEIL_PATH` → `~/.keil2clangd.json` →
scan of `D:/Keil_v5`, `C:/Keil_v5`, `C:/Keil` → interactive prompt (saved).

## CMSIS version selection

The script parses the device pack's `.pdsc` for CMSIS version requirements and
picks the closest installed version ≥ that. Otherwise the latest installed.

## Steps

### 1. Find the uvprojx and analyze ALL targets

```
Glob: **/*.uvprojx
```

If multiple are found, ask which one. Then read the XML and extract every
target's configuration — for each `<Target>`: `<TargetName>`,
`TargetArmAds/Cads/VariousControls/Define`, and `.../IncludePath`.

**Present a comparison table:**

| Target | Macros | Include path differences |
|--------|--------|------------------------|
| Iot-CSB-Debug_G048 | `__DEBUG, __G048` | `bsp/G048/...` |
| Iot-CSB-Release_LG048 | `__CODE_IAP, __LG048` | `bsp/LG048/...` |

**Key point:** targets often differ in chip-variant macros (`__G048` vs
`__LG048`), feature flags (`__CODE_IAP`, `USE_FULL_ASSERT`) and BSP paths. Ask
which target to generate for. If the target name contains a variant (`LG048`)
but its macros lack the matching define (`__LG048`), **check other targets and
warn** — that is a common Keil misconfiguration.

### 2. Run the script

```powershell
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/Keil2Clangd.py" -p <uvprojx_parent_dir> -o . -t <target_name>
```

Flag any warnings: empty project macros, MISSING include paths, Keil not found.

### 2b. Understand the two data sources (.uvprojx vs .dep)

- **`.uvprojx` (XML)** is the live source of truth for **macros (`-D`) and
  project include paths (`-I`)** — edit in Keil and it takes effect at once.
- **`.dep`** is written by Keil **after a build** and supplies only what XML
  cannot: compiler **system headers**, **preinclude** headers (`-imacros`), and
  the **real compiled file list**. Used automatically when fresh.
- **Staleness guard:** if `.uvprojx` is newer than `.dep`, the script prints
  `.dep: STALE ... ignored` and falls back to XML-only. Expected after a macro
  edit — rebuild, or ignore since macros come from XML anyway.
- Read the `.dep:` log line. `--no-dep` forces XML-only, `--dep-path` points at
  a non-standard output dir.

### 3. Validate macros (CRITICAL)

**3a. Cross-target macro analysis** — collect macros from ALL targets, find any
present in others but not the selected one, and check whether the codebase uses
them via `#ifdef`/`#if defined`/`#ifndef`. Pay special attention to chip-variant
macros.

**3b. Project macros** — every `Define` in the selected target's XML must appear
as `-D` in `.clangd`.

**3c. Compiler macros (auto-added)** — ARMCC v5 (uAC6=0) needs `__CC_ARM`,
`__arm__`, arch define; ARM Clang v6 (uAC6=1) needs `__ARMCC_VERSION=6000000`,
`__arm__`, arch define. Arch must match CPU: M0 → `__ARM_ARCH_6M__`,
M3 → `__ARM_ARCH_7M__`, M4/M7 → `__ARM_ARCH_7EM__`.

**3d. Hidden macros** — grep for `#ifdef`/`#if defined`/`#ifndef`, cross-check
against all targets plus auto-macros, list unresolved ones and ask.

### 4. Validate include paths

Check each `-I` exists. For Keil Pack paths, if the version mismatches, scan
`{keil}/ARM/PACK/{vendor}/{pack}/` for installed versions.

### 5. Validate compile_commands.json

Check each source exists, `directory` is correct, and includes/defines agree
with `.clangd`.

---

# IAR (.ewp)

Works with **any** IAR architecture. The compiler settings node is found by its
`ICC` prefix, so ICCARM, ICCRL78, ICCRX, ICC430 and friends all parse.

> The retired `Ewp2Json.py` matched a node named literally `ICCARM`. On any
> other architecture it parsed zero macros and zero include paths and still
> emitted a full compile_commands.json, so the output looked fine and was
> useless. It now forwards here.

## What makes the IAR backend different: it asks the compiler

Instead of hard-coding a macro table, the script runs the installed
`icc<arch>.exe` with `--predef_macros` and writes the result into a generated
preinclude header, wired up with `-imacros`. The macro set therefore always
matches the installed compiler version and options (300+ macros for RL78,
including `__ICCRL78__`, `__CORE__`, `__DATA_MODEL__`).

Things derived from that probe rather than guessed:

- **char signedness** — read from `__CHAR_MIN__`, emitted as
  `-funsigned-char`/`-fsigned-char`.
- **`--target` triple** — chosen by `__DEF_PTR_SIZE__`. A target is always
  emitted: with none, clang falls back to the host triple, and on Windows that
  is an MSVC triple whose predeclared `size_t` collides with IAR's target-sized
  one. Architectures clang has no backend for (RL78, RX, STM8) get a
  size-matched stand-in, and the stand-in's identity macros (`__MSP430__`, …)
  plus clang's own (`__GNUC__`, `__clang__`) are `#undef`'d in the generated
  header so code testing them is not fooled.
- **`--core`** — see below.

## Core negotiation

`.ewp` stores only the IDE dropdown *index* (`IccCore`), whose meaning varies by
architecture and workbench version. Instead of mapping it, the script uses the
compiler as the oracle: it compiles a TU that includes the project's device
header (derived from `GenDeviceSelect`, e.g. `R5F10WMG` → `ior5f10wmg.h`), first
with default options and then with each candidate core, keeping whichever is
accepted. Candidates come from the probe's own `__<ARCH>_<n>__` macros.

Getting this wrong is not subtle — device headers carry
`#error "... for use with ICCRL78 option --core rl78_1 only"`. Disable with
`--no-core-probe`, override with `--probe-args "--core s2"`.

## Extended keywords

`__near`, `__saddr`, `__interrupt`, `__no_init`, `__root`, … are language
extensions, not macros, so they never appear in `--predef_macros`. The generated
header shims them (`__weak` → `__attribute__((weak))`, most to nothing).

## Steps

### 1. List the build configurations

```powershell
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/Iar2Clangd.py" -p <dir> --list-configs
```

Configurations usually differ in macros the same way Keil targets do (`Debug` =
`_CODE_DEBUG_` vs `Releas_004` = `DEF_004`). Ask which one; the script also
prints a cross-configuration table and warns about macros present elsewhere but
not in the selected configuration.

### 2. Run

```powershell
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/Iar2Clangd.py" -p <dir> -o <output_dir> -c <configuration>
```

Review the report: unresolved `$VAR$` in include paths, MISSING directories,
whether the probe succeeded, which core was negotiated, which triple was picked.

### 3. Validate

Same as the Keil steps 3–5, plus:

- `-nostdinc` is emitted whenever IAR's own headers were found, so the standard
  library comes from the toolchain rather than the host. If IAR is **not**
  found, that flag is omitted — check the report and pass `--iar-path`.
- The `--dlib_config` from the project's `GenRTConfigPath` is fed to the probe,
  so `_DLIB_CONFIG_FILE_HEADER_NAME` matches the real build. `<toolkit>/lib` is
  added to the include path because that config header lives there.

## Known limitation: SFR names in vendor device headers

IAR device headers declare special function registers with two vendor
extensions at once:

```c
__saddr __no_init volatile union { unsigned char P0; __BITS8 P0_bit; } @ 0xFFF00;
```

clang cannot parse the `@ address` placement syntax, and even with it removed a
**file-scope anonymous union does not export its members** in C (verified: also
not under `-fms-extensions`). So SFR names do not resolve.

Mitigation in place: `-ferror-limit=0` is always emitted. Without it the first
19 parse errors abort the whole translation unit and *nothing* gets indexed;
with it the damage stays inside the vendor header. clangd also collapses
included-file errors, so the editor shows one squiggle at the `#include`, not
hundreds.

Measured on a 56-file RL78 project: 33 files (59%) clean apart from that single
header squiggle, 23 files (41%) — the BSP/register layer — still report
`use of undeclared identifier` for SFR names.

Fixing it properly means generating named `extern` declarations plus member
`#define`s from the device header and pre-defining its include guard so the
original body is skipped. Not implemented.

---

# CMake

CMake already emits a `compile_commands.json`; re-deriving one would only
produce a worse copy. What CMake does *not* do is make it findable — see the
placement section below, which is the entire reason this backend exists.

```powershell
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/Cmake2Clangd.py" -p <project_dir>
```

It runs `cmake -S <root> -B <build> -DCMAKE_EXPORT_COMPILE_COMMANDS=ON`,
preferring `-G Ninja` (multi-config IDE generators — Visual Studio, Xcode — do
not honour that switch and are refused up front), then locates the database,
checks it covers real files, and drops a pointer `.clangd` where clangd will
find it.

- `--no-configure` consumes an existing database instead of running cmake.
- `-b/--build-dir`, `-G/--generator`, `--cmake-args` pass through.
- If the database's compiler is a cross driver (`arm-none-eabi-gcc`, …), the
  script says so and prints the `CompileFlags.Compiler` + `--query-driver`
  incantation. It does not add them itself.

---

# Config discovery placement — applies to ALL backends (CRITICAL)

Validating content is not enough: clangd must be able to *find* the files.
**clangd searches a source file's own directory and its ANCESTOR directories
only — never sibling directories.**

The trap: output lands in the `Proj`/`build` folder while sources live in a
*sibling* dir (`Code`, `src`). The symptom is deceptive — **same-file navigation
still works, but cross-file jump-to-definition and find-references silently
fail**, because those need the background index, which needs the database.

All three backends now check this automatically and print
`placement: OK` or `placement: PROBLEM`. On PROBLEM, `--fix-placement`
(Keil/IAR) or the default behaviour (CMake) writes a pointer `.clangd` at the
sources' deepest common ancestor:

```yaml
CompileFlags:
  CompilationDatabase: <relative-path-to-the-dir-holding-compile_commands.json>
```

The pointer carries the `Diagnostics`/`ClangTidy` blocks too, since the real
`.clangd` sits where clangd will never read it for those sources. Place it at
the tightest ancestor that covers the sources but not unrelated sibling
projects (e.g. `App/`, not the repo root, so a separate `Boot/` is unaffected).

---

# Fix issues found

- **Missing macros**: add `-D` to `.clangd` `CompileFlags.Add`.
- **Missing include paths**: remove or correct.
- **Wrong Pack version**: update the version in the path.
- **ARMCC syntax extensions** clangd rejects — add only if clangd complains:
  - `__packed` → `-D__packed=__attribute__((packed))`
  - `__align(n)` → `-D__align(n)=__attribute__((aligned(n)))`
  - `__weak` → `-D__weak=__attribute__((weak))`
- **Excessive errors from vendor headers**: add specific `Diagnostics.Suppress`
  entries.

Then tell the user what was generated, what was fixed, what still needs
attention, and to restart clangd: Ctrl+Shift+P → "clangd: Restart language
server".

---

# Project moved / new machine (re-anchor)

Generated files contain machine/location-bound paths. Measured behavior
(clangd 22, Windows):

1. `compile_commands.json`'s `directory` MUST be a correct absolute path on the
   current machine — a relative value never works (clangd hard limit), and a
   stale absolute value only works while clangd's CWD happens to be the project
   root.
2. Relative `-I` in `.clangd` resolve against that `directory` anchor.
3. Absolute toolchain `-I` (e.g. `C:/Keil_v5/...`) break across machines — only
   re-probing can fix them.

```powershell
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/ReAnchor.py" --root <project_root>
# or double-click keil2clangd-reanchor.exe placed next to .clangd
# (build: scripts/build_exe.bat; needs no Python or plugin on the target machine)
```

**Ship the exe INSIDE the project (commit it)** — the point of the exe is to run
on machines with no Python and no plugin, so it must travel with the project.

Behavior:
- Same machine, moved folder: fully automatic — rewrites `directory` only.
- New machine: probes Keil (`KEIL_PATH` → `~/.keil2clangd.json` → common
  locations → prompt, saved) and rewrites dead toolchain `-I`/`-imacros`.
  Pack-version mismatches are kept + warned — re-run this skill instead.
- Surgical: relative `-I`, `-D` macros, comments and AI-added lines survive
  byte-for-byte. Originals backed up to `*.bak`. `--dry-run` previews.
- Out of scope: files generated with `-a`/`--absolute`, and project-local
  preinclude headers resolved to absolute paths. Regenerate instead.
- **IAR is not covered.** ReAnchor only knows Keil layouts; for a moved IAR
  project just re-run `Iar2Clangd.py`, which re-probes everything anyway.

Flags: `--root PATH`, `-k/--keil-path PATH`, `--dry-run`, `--no-pause`.

---

# Common issues the scripts can't handle

| Issue | Symptom | Fix |
|-------|---------|-----|
| Keil Pack version mismatch | MISSING pack include path | Scan Pack dir, update path |
| Macros defined in a batch build | `#ifdef` on undefined macro | Ask user, add `-D` |
| ARMCC `__packed`/`__align` | clangd syntax errors | Add `-D` compat macros |
| Multiple targets/configurations | Wrong macros for the user's build | Re-run with `-t`/`-c` |
| Vendor headers clangd can't parse | `fatal_too_many_errors` | `-ferror-limit=0` (IAR backend does this) |
| IAR SFR `@ address` declarations | `use of undeclared identifier 'P0'` | Not solved — see the IAR limitation section |
| Cross-drive paths (C: vs D:) | Relative path fails | Handled, but verify |
| Toolchain not found on a new machine | Prompted on first run | Enter path, saved to `~/.keil2clangd.json` |
| Output dir is a sibling of the sources | Same-file jump works, cross-file silently fails | `--fix-placement` |
| CMake configured with a VS generator | No `compile_commands.json` at all | `-G Ninja` |

# Script options

`Proj2Clangd.py`
```
-p, --path PATH       Directory to search (default: current dir)
--kind {keil,iar,cmake}   Force a backend instead of detecting one
--detect-only         Report what was found and exit
<everything else>     Forwarded to the backend
```

`Keil2Clangd.py`
```
-p PATH  -o PATH  -a/--absolute  -t/--target-name NAME  -k/--keil-path PATH
--no-clangd  --no-compile-commands  --no-dep  --dep-path PATH
--fix-placement  --dry-run
```

`Iar2Clangd.py`
```
-p PATH  -o PATH  -a/--absolute  -c/--config NAME (alias -t/--target-name)
--project PATH        Explicit .ewp, skipping the search
--iar-path PATH       Workbench root, e.g. ".../Embedded Workbench 8.0"
--iar-target TRIPLE   Override the clang --target ('' omits it)
--no-probe            Do not run the compiler for predefined macros
--probe-args "..."    Extra probe options, e.g. "--core s2 --data_model far"
--no-core-probe       Skip device-header core negotiation
--force-predef-header Write the preinclude header even with --no-probe
--list-configs  --no-clangd  --no-compile-commands  --fix-placement  --dry-run
```

`Cmake2Clangd.py`
```
-p PATH  -b/--build-dir PATH  -G/--generator NAME  --cmake PATH
--cmake-args "..."  --no-configure  -o PATH (pointer .clangd location)
--no-clangd  --dry-run
```

# Config file

`~/.keil2clangd.json` — auto-created, shared by the backends:
```json
{
  "keil_path": "D:\\Keil_v5",
  "iar_path": "D:\\Software\\IAR Systems\\Embedded Workbench 8.0"
}
```

# Generated files

| File | Backend | Note |
|---|---|---|
| `.clangd` | all | Flags + diagnostics, or a pointer when placement fails |
| `compile_commands.json` | Keil, IAR | CMake writes its own into the build dir |
| `k2c_iar_predef.h` | IAR | Probed macros + keyword shims, referenced via a **relative** `-imacros` so re-anchoring survives |
