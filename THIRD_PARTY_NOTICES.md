# Third-party notices

## QEMU

The OMAP5430 and Capricorn-M patches under `nix/patches/` modify QEMU
11.1.2 source fetched by the pinned Nix package. QEMU is distributed under GPL-2.0-or-later. The upstream
source and its notices are not copied into this repository. The license text
is included in [`licenses/GPL-2.0.txt`](licenses/GPL-2.0.txt).

## Vita3K USSE visitor

The optional shader arithmetic diagnostics in `tools/usse-recompiler/` reuse
the Vita3K USSE visitor from revision
`62727b58322ea7be10aaade575ac9ef82c58f5e8`, fetched by the `shader` Nix
development shell. Vita3K is distributed under GPL-2.0-or-later. Its license
text is included in [`licenses/GPL-2.0.txt`](licenses/GPL-2.0.txt); the upstream
source itself remains external to this repository.

## IDEmu virtual V850

`tools/qnx-v850-transport.c`, `tools/v850_link.py` and
`tools/v850-most-defaults.json` are derived from IDEmu
(<https://github.com/herrbimmer/IDEmu>, `src/v850/ioipc_idemu.c`,
`idemu/v850.py`, `idemu/most.py` and `idemu/data/hmi_property_defaults.json`)
and are licensed under the PolyForm Noncommercial License 1.0.0, included in
[`licenses/PolyForm-Noncommercial-1.0.0.txt`](licenses/PolyForm-Noncommercial-1.0.0.txt)
(<https://polyformproject.org/licenses/noncommercial/1.0.0>). They may be used
only for noncommercial purposes.

Required Notice: Copyright 2026 IDEmu contributors

The launcher also depends on QEMU, ANGLE, and other packages built from the
pinned Nixpkgs revision. Their source licenses and notices remain with those
upstream packages; this repository does not vendor their source.
