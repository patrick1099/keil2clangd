---
name: keil2clangd
description: Generate and validate .clangd + compile_commands.json from Keil .uvprojx (or IAR .ewp) project files for embedded C projects. Use when setting up clangd-based jump/completion/diagnostics for a Keil MDK or IAR firmware project, or when clangd reports missing macros / include paths / ARMCC syntax errors.
---

# Keil to Clangd Configuration Generator

Generate `.clangd` and `compile_commands.json` from a Keil `.uvprojx` project file, then validate the output and fix issues the script can't handle.

The bundled script is at `${CLAUDE_PLUGIN_ROOT}/scripts/Keil2Clangd.py` (IAR variant: `Ewp2Json.py`; legacy JSON-only: `Keil2Json.py`). Run it with `py -3` (Windows) or `python3`.

## Keil path configuration

The script persists the Keil installation path to `~/.keil2clangd.json`. Discovery priority:
1. `-k` / `--keil-path` CLI argument
2. `KEIL_PATH` environment variable
3. `~/.keil2clangd.json` (auto-saved on first discovery)
4. Fallback scan of common locations (`D:/Keil_v5`, `C:/Keil_v5`, `C:/Keil`)
5. Interactive prompt (saves to config file)

If Keil is found automatically or entered interactively, the path is saved so future runs on any project reuse it without re-entering.

## CMSIS version selection

The script parses the device pack's `.pdsc` file for CMSIS version requirements. If a hint is found, it selects the closest installed version >= that requirement. Otherwise falls back to the latest installed version.

## Steps

### 1. Find the uvprojx file and analyze ALL targets

```
Glob: **/*.uvprojx
```

If multiple uvprojx found, ask the user which one to use.

**Then read the uvprojx XML and extract EVERY target's configuration:**

For each `<Target>` element in the XML:
- `<TargetName>` — target name
- `TargetArmAds/Cads/VariousControls/Define` — that target's macros
- `TargetArmAds/Cads/VariousControls/IncludePath` — that target's include paths

**Present a comparison table to the user**, like:

| Target | Macros | Include path differences |
|--------|--------|------------------------|
| Iot-CSB-Debug_G048 | `__DEBUG, __G048` | `bsp/G048/...` |
| Iot-CSB-Debug_LG048 | `USE_FULL_ASSERT, __DEBUG` | `bsp/LG048/...` |
| Iot-CSB-Release_LG048 | `__CODE_IAP, __LG048` | `bsp/LG048/...` |

**Key point:** Different targets often have different chip variant macros (e.g. `__G048` vs `__LG048`), feature flags (`__CODE_IAP`, `USE_FULL_ASSERT`), and BSP paths. The target name often hints at which chip/variant it's for — use this to identify which macros the user actually needs.

Ask the user which target to generate for. If the target name contains a chip variant (e.g. `LG048` in `Iot-CSB-Debug_LG048`) but the target's macros don't include the corresponding define (e.g. missing `__LG048`), **check other targets for that macro and warn the user**. This is a common Keil misconfiguration — the macro may need to be added manually.

### 2. Run the script

```powershell
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/Keil2Clangd.py" -p <uvprojx_parent_dir> -o . -t <target_name>
```

Review the macro check output carefully. Flag any warnings to the user:
- Empty project macros
- MISSING include paths
- Keil installation not found (if not in `~/.keil2clangd.json` and not auto-detected)

### 3. Validate macros (CRITICAL)

Read the generated `.clangd` and cross-check macros:

**3a. Cross-target macro analysis:**
- Collect macros from ALL targets in the uvprojx
- Identify macros that appear in OTHER targets but not the selected one
- For each missing macro, check if source code uses it via `#ifdef`/`#if defined`/`#ifndef`
- If a macro from another target is used in the codebase, it's likely needed — ask user whether to add it
- Pay special attention to chip variant macros: if target name says `LG048` but macros don't include `__LG048`, check Release or other targets for it

**3b. Project macros from selected target:**
- Read the `.uvprojx` XML, find the selected target's `VariousControls/Define`
- Every define must appear as `-D` in `.clangd`

**3c. Compiler macros (auto-added by script):**
- ARMCC v5 (uAC6=0): must have `__CC_ARM`, `__arm__`, arch define
- ARM Clang v6 (uAC6=1): must have `__ARMCC_VERSION=6000000`, `__arm__`, arch define
- Arch define must match CPU: Cortex-M0 -> `__ARM_ARCH_6M__`, M3 -> `__ARM_ARCH_7M__`, M4/M7 -> `__ARM_ARCH_7EM__`

**3d. Hidden macros not in any target:**
- Grep source files for `#ifdef` / `#if defined` / `#ifndef` patterns
- Cross-reference found macros against ALL targets' defines + compiler auto-macros
- If unresolved macros found, list them and ask the user which ones to add

### 4. Validate include paths

For each `-I` path in `.clangd`:
- Check the path exists on disk using Glob or Bash
- If MISSING: warn and suggest alternatives
- For Keil Pack paths: if version mismatch, scan `{keil}/ARM/PACK/{vendor}/{pack}/` for installed versions

### 5. Validate compile_commands.json

- Check each source file in `compile_commands.json` exists
- Verify `directory` field is correct
- Check includes and defines are consistent with `.clangd`

### 6. Fix issues found

For any problems discovered in steps 3-5:
- **Missing macros**: Add `-D` flags to `.clangd` CompileFlags.Add section using Edit
- **Missing include paths**: Remove non-existent paths or fix to correct location
- **Wrong Pack version**: Update version in path
- **ARMCC-specific syntax errors**: Common Keil ARMCC extensions that clangd doesn't understand:
  - `__packed` -> add `-D__packed=__attribute__((packed))`
  - `__align(n)` -> add `-D__align(n)=__attribute__((aligned(n)))`
  - `__weak` -> add `-D__weak=__attribute__((weak))`
  - Add these to `.clangd` only if clangd reports errors on them
- **Excessive clangd errors**: If too many errors from Keil headers, add specific diagnostics to Suppress

### 7. Report and restart

Tell the user:
- Summary of what was generated
- Any issues found and fixed
- Any remaining warnings that need manual attention
- Instruct to restart clangd: Ctrl+Shift+P -> "clangd: Restart language server"

## Common issues the script can't handle

| Issue | Symptom | Fix |
|-------|---------|-----|
| Keil Pack version mismatch | MISSING pack include path | Scan Pack dir for installed version, update path |
| Macros defined in batch build | `#ifdef` on undefined macro | Ask user, add `-D` to .clangd |
| ARMCC __packed/__align syntax | clangd syntax errors | Add `-D` compatibility macros |
| Multiple targets, different configs | Wrong macros for user's build | Re-run with `--target-name` |
| Keil headers clangd can't parse | `fatal_too_many_errors` | Switch to ARMCLANG headers or add Suppress rules |
| Cross-drive paths (C: vs D:) | Relative path fails | Script handles this, but verify |
| Keil not found on new machine | Prompted for path on first run | Enter path, auto-saved to `~/.keil2clangd.json` |
| Wrong CMSIS version selected | Include path points to wrong version | Script reads .pdsc for version hint; verify in output |

## Script location

Bundled with this plugin: `${CLAUDE_PLUGIN_ROOT}/scripts/Keil2Clangd.py`
(IAR projects: `${CLAUDE_PLUGIN_ROOT}/scripts/Ewp2Json.py`)

## Script options

```
-p, --path PATH         Search path for .uvprojx (default: current dir)
-a, --absolute          Use absolute paths
-t, --target-name NAME  Select specific Target in multi-target project
-k, --keil-path PATH    Keil installation path (overrides ~/.keil2clangd.json)
--no-clangd             Skip .clangd generation
--no-compile-commands   Skip compile_commands.json generation
--dry-run               Print info without writing files
-o, --output PATH       Output directory (default: current dir)
```

## Config file

`~/.keil2clangd.json` — auto-created on first run, stores Keil path for all projects:
```json
{
  "keil_path": "D:\\Keil_v5"
}
```
