#!/bin/bash
# The Os_clang ELF: the C sources compiled file by file with clang -Os, using the
# flags of Dilithium-LLVM/llvm/llvm-gen/llvm-gen.cpp (which uses -O0).
#   firmware/build_os_clang.sh <Dilithium-LLVM dir> <out.elf>
# Needs clang (18 was used) and arm-none-eabi-gcc with newlib.
set -e
[ $# -eq 2 ] || { echo "usage: $0 <Dilithium-LLVM dir> <out.elf>" >&2; exit 2; }
HERE=$(cd "$(dirname "$0")" && pwd)
REF=$1/ref; OUT=$2; OBJ=$OUT.objs
CLANG=${CLANG:-clang-18}; GCC=${GCC:-arm-none-eabi-gcc}
NEWLIB=${NEWLIB:-/usr/include/newlib}
GCCINC=${GCCINC:-$($GCC -print-file-name=include)}
mkdir -p "$OBJ"
for f in "$REF"/test/test_dilithium.c "$REF"/{randombytes,sign,polyvec,poly,ntt,reduce,fips202,symmetric-shake,packing,rounding}.c \
         "$1"/llvm/llvm-gen/mem.c; do
  "$CLANG" --target=thumbv7em-unknown-none-eabihf -mcpu=cortex-m4 -mfloat-abi=hard -mfpu=fpv4-sp-d16 -mabi=aapcs \
    -Os -g -ffreestanding -fno-builtin -Wno-macro-redefined -DDILITHIUM_MODE=2 -I"$REF" \
    -isystem "$NEWLIB" -isystem "$GCCINC" -c "$f" -o "$OBJ/$(basename "$f" .c).o"
done
ARM="-mcpu=cortex-m4 -mthumb -mfloat-abi=hard -mfpu=fpv4-sp-d16"
$GCC $ARM -O1 -c "$HERE/startup.c" -o "$OBJ/startup.o"
$GCC $ARM -nostartfiles --specs=nosys.specs -T "$HERE/mps2.ld" "$OBJ"/*.o -o "$OUT" -Wl,--no-warn-rwx-segments,--no-enum-size-warning
echo "$OUT"
