#!/usr/bin/env bash
# Build the emulator-only io-ipc V850 transport (see qnx-v850-transport.c).
set -euo pipefail

if [[ $# != 1 ]]; then
  echo "Usage: $0 OUTPUT_LIBRARY" >&2
  exit 2
fi
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
scratch="$(mktemp -d "${TMPDIR:-/tmp}/idrive-v850-transport.XXXXXX")"
trap 'rm -rf "$scratch"' EXIT
clang="${IDRIVE_QNX_CLANG:-clang}"
flags=(--target=armv7-none-eabi -march=armv7-a -mfloat-abi=soft -fPIC -ffreestanding
       -fno-stack-protector -mno-unaligned-access -Os)
"$clang" "${flags[@]}" -std=c11 -Wall -Wextra -Werror \
  -c "$script_dir/qnx-v850-transport.c" -o "$scratch/transport.o"
"$clang" "${flags[@]}" -DIDRIVE_V850_TRANSPORT_LINK \
  -c "$script_dir/qnx-libc-link-stub.c" -o "$scratch/libc.o"
ld.lld -m armelf -shared -soname libc.so.3 -o "$scratch/libc.so" "$scratch/libc.o"
# ioipc_bp_* come from the io-ipc executable that loads the transport.
ld.lld -m armelf -shared -soname ioipc-idrive-v850.so -z norelro --hash-style=sysv \
  --allow-shlib-undefined -o "$1" "$scratch/transport.o" "$scratch/libc.so"
echo "Built QNX io-ipc V850 transport: $1"
