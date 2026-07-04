#!/usr/bin/env python3
"""
Keil2Clangd - Generate .clangd and compile_commands.json from Keil .uvprojx files.

Parses Keil MDK project files and generates clangd-compatible configuration
for embedded C projects using ARMCC v5 or ARM Clang v6.
"""

import os
import re
import json
import argparse
import xml.etree.ElementTree as ET
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional


# ---------------------------------------------------------------------------
# Mapping tables
# ---------------------------------------------------------------------------

CPU_TARGET_MAP = {
    "Cortex-M0":  "armv6m-none-eabi",
    "Cortex-M0+": "armv6m-none-eabi",
    "Cortex-M3":  "armv7m-none-eabi",
    "Cortex-M4":  "armv7em-none-eabi",
    "Cortex-M7":  "armv7em-none-eabi",
    "Cortex-M23": "armv8m.base-none-eabi",
    "Cortex-M33": "armv8m.main-none-eabi",
}

CPU_ARCH_DEFINE_MAP = {
    "Cortex-M0":  "__ARM_ARCH_6M__",
    "Cortex-M0+": "__ARM_ARCH_6M__",
    "Cortex-M3":  "__ARM_ARCH_7M__",
    "Cortex-M4":  "__ARM_ARCH_7EM__",
    "Cortex-M7":  "__ARM_ARCH_7EM__",
    "Cortex-M23": "__ARM_ARCH_8M_BASE__",
    "Cortex-M33": "__ARM_ARCH_8M_MAIN__",
}

KEIL_FALLBACK_PATHS = ["D:/Keil_v5", "C:/Keil_v5", "C:/Keil"]
CONFIG_FILE = Path.home() / '.keil2clangd.json'


# ---------------------------------------------------------------------------
# UvprojxParser
# ---------------------------------------------------------------------------

class UvprojxParser:
    """Parse a Keil .uvprojx project file and extract build configuration."""

    def __init__(self, file_path, target_name=None):
        self.file_path = Path(file_path).resolve()
        self.project_root = self.file_path.parent
        self.tree = ET.parse(str(self.file_path))
        self.root = self.tree.getroot()
        self.target_name = target_name
        self.target = self._find_target()

    def _find_target(self):
        """Find a specific target by name, or return the first target."""
        targets = self.root.findall('.//Target')
        if not targets:
            raise ValueError(f"No targets found in {self.file_path}")

        if self.target_name:
            for t in targets:
                name_elem = t.find('TargetName')
                if name_elem is not None and name_elem.text == self.target_name:
                    return t
            available = [t.find('TargetName').text for t in targets
                         if t.find('TargetName') is not None]
            raise ValueError(
                f"Target '{self.target_name}' not found. "
                f"Available targets: {available}"
            )
        return targets[0]

    def get_target_name(self):
        elem = self.target.find('TargetName')
        return elem.text if elem is not None else "Unknown"

    def list_targets(self):
        targets = self.root.findall('.//Target')
        names = []
        for t in targets:
            name_elem = t.find('TargetName')
            if name_elem is not None and name_elem.text:
                names.append(name_elem.text)
        return names

    def get_cpu_type(self):
        """Extract CPU type from AdsCpuType or fall back to Cpu CPUTYPE regex."""
        # Try AdsCpuType first
        elem = self.target.find('.//TargetArmAds/ArmAdsMisc/AdsCpuType')
        if elem is not None and elem.text:
            # Strip surrounding quotes if present
            return elem.text.strip().strip('"')

        # Fallback: parse from Cpu element
        cpu_elem = self.target.find('.//TargetCommonOption/Cpu')
        if cpu_elem is not None and cpu_elem.text:
            match = re.search(r'CPUTYPE\("([^"]+)"\)', cpu_elem.text)
            if match:
                return match.group(1)

        return None

    def get_compiler_info(self):
        """Return dict with is_ac6 (bool) and version_string."""
        uac6_elem = self.target.find('uAC6')
        is_ac6 = False
        if uac6_elem is not None and uac6_elem.text:
            is_ac6 = uac6_elem.text.strip() == '1'

        version_string = "ARMCC v5"
        pcc_elem = self.target.find('pCCUsed')
        if pcc_elem is not None and pcc_elem.text:
            version_string = pcc_elem.text.strip()

        return {"is_ac6": is_ac6, "version_string": version_string}

    def get_pack_id(self):
        elem = self.target.find('.//TargetCommonOption/PackID')
        if elem is not None and elem.text:
            return elem.text.strip()
        return None

    def get_defines(self):
        """Get project defines (comma-separated in the XML)."""
        elem = self.target.find('.//TargetArmAds/Cads/VariousControls/Define')
        if elem is not None and elem.text:
            return [d.strip() for d in elem.text.split(',') if d.strip()]
        return []

    def get_include_paths(self):
        """Get include paths (semicolon-separated), resolved to absolute."""
        elem = self.target.find('.//TargetArmAds/Cads/VariousControls/IncludePath')
        if elem is None or not elem.text:
            return []
        raw_paths = elem.text.split(';')
        abs_paths = []
        seen = set()
        for p in raw_paths:
            p = p.strip().replace('\\', '/')
            if not p:
                continue
            resolved = (self.project_root / p).resolve()
            key = str(resolved).lower()
            if key not in seen:
                seen.add(key)
                abs_paths.append(resolved)
        return abs_paths

    def get_source_files(self):
        """Find all source file paths, resolved to absolute."""
        files = []
        for group in self.root.findall('.//Group'):
            for file_elem in group.findall('.//File'):
                fp_elem = file_elem.find('FilePath')
                if fp_elem is not None and fp_elem.text:
                    raw = fp_elem.text.strip().replace('\\', '/')
                    resolved = (self.project_root / raw).resolve()
                    files.append(resolved)
        return files

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


# ---------------------------------------------------------------------------
# KeilPathResolver
# ---------------------------------------------------------------------------

class KeilPathResolver:
    """Locate Keil installation and resolve compiler / pack include paths."""

    def __init__(self, keil_path=None):
        self.keil_root = None

        # 1. Explicit CLI path
        if keil_path and Path(keil_path).is_dir():
            self.keil_root = Path(keil_path).resolve()
            return

        # 2. Environment variable
        env_path = os.environ.get('KEIL_PATH')
        if env_path and Path(env_path).is_dir():
            self.keil_root = Path(env_path).resolve()
            return

        # 3. User config file (~/.keil2clangd.json)
        config_path = self._load_config_keil_path()
        if config_path and Path(config_path).is_dir():
            self.keil_root = Path(config_path).resolve()
            return

        # 4. Fallback: search common locations
        for sp in KEIL_FALLBACK_PATHS:
            if Path(sp).is_dir():
                self.keil_root = Path(sp).resolve()
                self._save_config_keil_path(str(self.keil_root))
                print(f"Found Keil at {self.keil_root}, saved to {CONFIG_FILE}")
                return

        # 5. Interactive prompt
        self._prompt_and_save()

    @staticmethod
    def _load_config():
        if CONFIG_FILE.is_file():
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    @staticmethod
    def _save_config(config):
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)

    @classmethod
    def _load_config_keil_path(cls):
        return cls._load_config().get('keil_path')

    @classmethod
    def _save_config_keil_path(cls, keil_path):
        config = cls._load_config()
        config['keil_path'] = keil_path
        cls._save_config(config)

    def _prompt_and_save(self):
        print("Keil installation not found automatically.")
        print("Please enter the Keil installation path (e.g. D:/Keil_v5):")
        try:
            user_path = input("> ").strip()
        except EOFError:
            return
        if user_path and Path(user_path).is_dir():
            self.keil_root = Path(user_path).resolve()
            self._save_config_keil_path(str(self.keil_root))
            print(f"Saved to {CONFIG_FILE}")
        else:
            print(f"WARNING: '{user_path}' is not a valid directory.")

    def found(self):
        return self.keil_root is not None

    def get_compiler_includes(self, is_ac6):
        """Return list of existing compiler include directories."""
        if not self.found():
            return []
        paths = []
        candidates = [
            self.keil_root / "ARM" / "ARMCLANG" / "include",
        ]
        for c in candidates:
            if c.is_dir():
                paths.append(c)
        return paths

    def _find_cmsis_version_from_pdsc(self, vendor, pack_name, version):
        """Try to find required CMSIS version from device pack's .pdsc file."""
        pack_dir = self.keil_root / "ARM" / "PACK" / vendor / pack_name / version
        pdsc_files = list(pack_dir.glob("*.pdsc")) if pack_dir.is_dir() else []
        if not pdsc_files:
            return None

        try:
            tree = ET.parse(str(pdsc_files[0]))
            root = tree.getroot()
            ns = ''
            if root.tag.startswith('{'):
                ns = root.tag.split('}')[0] + '}'
            # Check <require Cclass="CMSIS" Cversion="..."/>
            for req in root.iter(f'{ns}require'):
                if req.get('Cclass') == 'CMSIS' and req.get('Cversion'):
                    return req.get('Cversion')
            # Check <package vendor="ARM" name="CMSIS" version="..."/>
            for pkg in root.iter(f'{ns}package'):
                if pkg.get('vendor') == 'ARM' and pkg.get('name') == 'CMSIS':
                    ver = pkg.get('version', '')
                    if ver:
                        return ver.split(':')[0]
        except ET.ParseError:
            pass
        return None

    @staticmethod
    def _parse_pack_id(pack_id):
        """Parse PackID into (vendor, pack_name, version) or None."""
        parts = pack_id.split('.')
        if len(parts) < 4:
            return None

        vendor = parts[0]
        version_start = None
        for i in range(len(parts) - 2):
            if (parts[i].isdigit() and parts[i + 1].isdigit()
                    and parts[i + 2].isdigit()
                    and i + 2 == len(parts) - 1):
                version_start = i
                break

        if version_start is None:
            return None

        pack_name = '.'.join(parts[1:version_start])
        version = '.'.join(parts[version_start:])
        return vendor, pack_name, version

    def get_pack_includes(self, pack_id):
        """Parse PackID and return existing pack include directories."""
        if not self.found() or not pack_id:
            return []

        parsed = self._parse_pack_id(pack_id)
        if not parsed:
            return []
        vendor, pack_name, version = parsed

        paths = []

        # Device include
        device_inc = (self.keil_root / "ARM" / "PACK" / vendor
                      / pack_name / version / "Device" / "Include")
        if device_inc.is_dir():
            paths.append(device_inc)

        # CMSIS Core Include — prefer version matching device pack .pdsc hint
        cmsis_base = self.keil_root / "ARM" / "PACK" / "ARM" / "CMSIS"
        if cmsis_base.is_dir():
            installed = sorted(
                [d for d in cmsis_base.iterdir() if d.is_dir()],
                key=lambda d: d.name,
            )
            chosen = None

            hint = self._find_cmsis_version_from_pdsc(vendor, pack_name, version)
            if hint:
                for d in installed:
                    if d.name >= hint:
                        chosen = d
                        break

            if chosen is None and installed:
                chosen = installed[-1]

            if chosen:
                core_inc = chosen / "CMSIS" / "Core" / "Include"
                if core_inc.is_dir():
                    paths.append(core_inc)

        return paths


# ---------------------------------------------------------------------------
# Path formatting helper
# ---------------------------------------------------------------------------

def _format_path(abs_path, base_dir, use_absolute):
    """Format a path as relative or absolute, with forward slashes."""
    abs_path = Path(abs_path).resolve()
    if use_absolute:
        return str(abs_path).replace('\\', '/')
    try:
        rel = os.path.relpath(str(abs_path), str(base_dir))
        return rel.replace('\\', '/')
    except ValueError:
        # Cross-drive on Windows
        return str(abs_path).replace('\\', '/')


# ---------------------------------------------------------------------------
# ClangdGenerator
# ---------------------------------------------------------------------------

class ClangdGenerator:
    """Generate a .clangd YAML configuration file."""

    CLANG_TIDY_ADD = [
        "bugprone-*",
        "readability-*",
        "performance-*",
    ]

    CLANG_TIDY_REMOVE = [
        "readability-magic-numbers",
        "readability-identifier-length",
        "bugprone-easily-swappable-parameters",
        "performance-no-int-to-ptr",
    ]

    DIAGNOSTICS_SUPPRESS = [
        "-Wunused-parameter",
        "-Wmissing-prototypes",
        "-Wstrict-prototypes",
    ]

    def __init__(self, parser, keil_resolver, use_absolute=False, base_dir=None):
        self.parser = parser
        self.keil = keil_resolver
        self.use_absolute = use_absolute
        self.base_dir = Path(base_dir).resolve() if base_dir else Path.cwd().resolve()

    def generate(self):
        """Return the .clangd YAML string."""
        cpu = self.parser.get_cpu_type()
        compiler_info = self.parser.get_compiler_info()
        pack_id = self.parser.get_pack_id()
        defines = self.parser.get_defines()
        include_paths = self.parser.get_include_paths()

        lines = []
        lines.append("CompileFlags:")
        lines.append("  Add:")

        # Target
        target = CPU_TARGET_MAP.get(cpu, "armv6m-none-eabi")
        lines.append(f"    # {cpu}")
        lines.append(f"    - --target={target}")

        # Compiler compatibility macros
        lines.append("    # ARM C Compiler compatibility macros")
        if compiler_info["is_ac6"]:
            lines.append("    - -D__ARMCC_VERSION=6000000")
        else:
            lines.append("    - -D__CC_ARM")
        lines.append("    - -D__arm__")
        arch_define = CPU_ARCH_DEFINE_MAP.get(cpu)
        if arch_define:
            lines.append(f"    - -D{arch_define}")

        # Project macros
        if defines:
            lines.append("    # Keil project macros")
            for d in defines:
                lines.append(f"    - -D{d}")

        # Project include paths
        lines.append("    # Include paths")
        for p in include_paths:
            formatted = _format_path(p, self.base_dir, self.use_absolute)
            lines.append(f"    - -I{formatted}")

        # Keil compiler and pack includes
        if self.keil.found():
            compiler_incs = self.keil.get_compiler_includes(
                compiler_info["is_ac6"])
            pack_incs = self.keil.get_pack_includes(pack_id)

            keil_incs = compiler_incs + pack_incs
            if keil_incs:
                lines.append("    # Keil/ARMCC standard library and CMSIS/device headers for clangd.")
                for ki in keil_incs:
                    formatted = _format_path(ki, self.base_dir, self.use_absolute)
                    lines.append(f"    - -I{formatted}")

        # Remove flags
        lines.append("  Remove:")
        lines.append("    # Drop warning flags that are noisy for embedded code")
        lines.append("    - -W*")
        lines.append("    - -pedantic")

        # Diagnostics
        lines.append("")
        lines.append("Diagnostics:")
        lines.append("  Suppress:")
        for s in self.DIAGNOSTICS_SUPPRESS:
            lines.append(f"    - {s}")
        lines.append("  ClangTidy:")
        lines.append("    Add:")
        for a in self.CLANG_TIDY_ADD:
            lines.append(f"      - {a}")
        lines.append("    Remove:")
        for r in self.CLANG_TIDY_REMOVE:
            lines.append(f"      - {r}")

        return '\n'.join(lines) + '\n'

    def write(self, output_path):
        content = self.generate()
        out = Path(output_path) / '.clangd'
        out.write_text(content, encoding='utf-8')
        print(f"Generated: {out}")


# ---------------------------------------------------------------------------
# CompileCommandsGenerator
# ---------------------------------------------------------------------------

class CompileCommandsGenerator:
    """Generate compile_commands.json for clangd / IDE integration."""

    def __init__(self, parser, keil_resolver, use_absolute=False, base_dir=None,
                 enrichment=None):
        self.parser = parser
        self.keil = keil_resolver
        self.use_absolute = use_absolute
        self.base_dir = Path(base_dir).resolve() if base_dir else Path.cwd().resolve()
        self.enrichment = enrichment

    def generate(self):
        """Return a list of compile-command entry dicts."""
        cpu = self.parser.get_cpu_type()
        compiler_info = self.parser.get_compiler_info()
        pack_id = self.parser.get_pack_id()
        defines = self.parser.get_defines()
        include_paths = self.parser.get_include_paths()
        source_files = self.parser.get_source_files()
        enr = self.enrichment
        use_enr = bool(enr and enr.found and not enr.stale)
        if use_enr and enr.source_files:
            source_files = enr.source_files

        target = CPU_TARGET_MAP.get(cpu, "armv6m-none-eabi")
        arch_define = CPU_ARCH_DEFINE_MAP.get(cpu)

        dir_str = str(self.base_dir).replace('\\', '/')

        # Build common arguments
        base_args = [f"--target={target}"]

        # Compiler macros
        if compiler_info["is_ac6"]:
            base_args.append("-D__ARMCC_VERSION=6000000")
        else:
            base_args.append("-D__CC_ARM")
        base_args.append("-D__arm__")
        if arch_define:
            base_args.append(f"-D{arch_define}")

        # Project defines
        for d in defines:
            base_args.append(f"-D{d}")

        # Project includes
        for p in include_paths:
            formatted = _format_path(p, self.base_dir, self.use_absolute)
            base_args.append(f"-I{formatted}")

        # Keil includes
        if self.keil.found():
            compiler_incs = self.keil.get_compiler_includes(
                compiler_info["is_ac6"])
            pack_incs = self.keil.get_pack_includes(pack_id)
            for ki in compiler_incs + pack_incs:
                formatted = _format_path(ki, self.base_dir, self.use_absolute)
                base_args.append(f"-I{formatted}")

        # .dep enrichment: compiler system includes (XML can't provide these)
        if use_enr:
            existing = {a for a in base_args if a.startswith("-I")}
            for inc in enr.system_includes:
                formatted = _format_path(inc, self.base_dir, self.use_absolute)
                flag = f"-I{formatted}"
                if flag not in existing:
                    base_args.append(flag)
                    existing.add(flag)

        compiler = "arm-none-eabi-gcc"

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

    def write(self, output_path):
        entries = self.generate()
        out = Path(output_path) / 'compile_commands.json'
        with open(str(out), 'w', encoding='utf-8') as f:
            json.dump(entries, f, indent=4, ensure_ascii=False)
        print(f"Generated: {out}  ({len(entries)} entries)")


# ---------------------------------------------------------------------------
# Macro checker
# ---------------------------------------------------------------------------

def check_macros(parser, keil_resolver):
    """Print diagnostic info about the parsed project."""
    cpu = parser.get_cpu_type()
    compiler_info = parser.get_compiler_info()
    pack_id = parser.get_pack_id()
    defines = parser.get_defines()
    include_paths = parser.get_include_paths()

    print("=" * 60)
    print(f"  Target:    {parser.get_target_name()}")
    print(f"  CPU:       {cpu}")
    print(f"  Compiler:  {'AC6 (armclang)' if compiler_info['is_ac6'] else 'AC5 (armcc)'}"
          f"  [{compiler_info['version_string']}]")
    print(f"  PackID:    {pack_id}")
    print(f"  Clang target: {CPU_TARGET_MAP.get(cpu, '???')}")
    print("=" * 60)

    # Project macros
    print(f"\n[Project macros] ({len(defines)} found)")
    if defines:
        for d in defines:
            print(f"  -D{d}")
    else:
        print("  WARNING: no project macros found in uvprojx!")

    # Compiler macros (auto-added)
    auto_macros = []
    if compiler_info["is_ac6"]:
        auto_macros.append("__ARMCC_VERSION=6000000")
    else:
        auto_macros.append("__CC_ARM")
    auto_macros.append("__arm__")
    arch_def = CPU_ARCH_DEFINE_MAP.get(cpu)
    if arch_def:
        auto_macros.append(arch_def)

    print(f"\n[Auto-added compiler macros] ({len(auto_macros)})")
    for m in auto_macros:
        print(f"  -D{m}")

    total = len(defines) + len(auto_macros)
    print(f"\n  Total macros: {total}")

    # Include paths
    print(f"\n[Project include paths] ({len(include_paths)})")
    for p in include_paths:
        exists = p.is_dir()
        marker = "OK" if exists else "MISSING"
        print(f"  [{marker}] {p}")

    # Keil paths
    if keil_resolver.found():
        print(f"\n[Keil installation] {keil_resolver.keil_root}")
        compiler_incs = keil_resolver.get_compiler_includes(
            compiler_info["is_ac6"])
        pack_incs = keil_resolver.get_pack_includes(pack_id)
        all_keil = compiler_incs + pack_incs
        print(f"[Keil include paths] ({len(all_keil)})")
        for ki in all_keil:
            exists = ki.is_dir()
            marker = "OK" if exists else "MISSING"
            print(f"  [{marker}] {ki}")
    else:
        print("\n[Keil installation] NOT FOUND")

    # All targets with their macros
    targets = parser.list_targets()
    if len(targets) > 1:
        print(f"\n[All targets and their macros] ({len(targets)})")
        selected_name = parser.get_target_name()
        all_target_defines = {}
        for t_name in targets:
            t_parser = UvprojxParser(str(parser.file_path), target_name=t_name)
            t_defines = t_parser.get_defines()
            all_target_defines[t_name] = set(t_defines)
            cur = " <-- selected" if t_name == selected_name else ""
            macros_str = ", ".join(t_defines) if t_defines else "(none)"
            print(f"  {t_name}{cur}")
            print(f"    Macros: {macros_str}")

        # Warn about macros in other targets but missing from selected
        selected_defines = all_target_defines.get(selected_name, set())
        other_macros = set()
        for t_name, t_defs in all_target_defines.items():
            if t_name != selected_name:
                other_macros |= t_defs
        missing_from_selected = other_macros - selected_defines
        if missing_from_selected:
            print(f"\n[WARN] Macros in other targets but NOT in '{selected_name}':")
            for m in sorted(missing_from_selected):
                sources = [t for t, d in all_target_defines.items()
                           if m in d and t != selected_name]
                print(f"  -D{m}  (from: {', '.join(sources)})")

    print()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Generate .clangd and compile_commands.json from Keil .uvprojx"
    )
    ap.add_argument('-p', '--path', default='.',
                    help='Search path for .uvprojx file (default: current dir)')
    ap.add_argument('-a', '--absolute', action='store_true',
                    help='Use absolute paths in generated files')
    ap.add_argument('-t', '--target-name', default=None,
                    help='Select a specific build target by name')
    ap.add_argument('-k', '--keil-path', default=None,
                    help='Keil installation path (e.g. D:/Keil_v5)')
    ap.add_argument('--no-clangd', action='store_true',
                    help='Skip .clangd generation')
    ap.add_argument('--no-compile-commands', action='store_true',
                    help='Skip compile_commands.json generation')
    ap.add_argument('--dry-run', action='store_true',
                    help='Print info without writing any files')
    ap.add_argument('-o', '--output', default='.',
                    help='Output directory (default: current dir)')

    args = ap.parse_args()

    # Find .uvprojx file
    search_path = Path(args.path).resolve()
    uvprojx_files = list(search_path.glob('**/*.uvprojx'))
    if not uvprojx_files:
        print(f"ERROR: No .uvprojx file found under {search_path}")
        return 1

    uvprojx_path = uvprojx_files[0]
    print(f"Using: {uvprojx_path}")

    # Parse
    parser = UvprojxParser(str(uvprojx_path), target_name=args.target_name)

    # Resolve Keil
    keil = KeilPathResolver(keil_path=args.keil_path)

    # Output directory
    output_dir = Path(args.output).resolve()

    # Always print macro / path check
    check_macros(parser, keil)

    if args.dry_run:
        print("--dry-run: no files written.")
        return 0

    # Generate
    if not args.no_clangd:
        gen = ClangdGenerator(parser, keil,
                              use_absolute=args.absolute,
                              base_dir=output_dir)
        gen.write(output_dir)

    if not args.no_compile_commands:
        gen = CompileCommandsGenerator(parser, keil,
                                       use_absolute=args.absolute,
                                       base_dir=output_dir)
        gen.write(output_dir)

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
