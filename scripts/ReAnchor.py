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
