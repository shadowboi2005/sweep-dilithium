#!/usr/bin/env python3
"""Checks of the fault injection itself: skips inside IT blocks, and that a
trial leaves nothing behind for the next one.  python test_skip.py"""
import os
import struct

from emu import FLASH, EmulationError, Machine, Skip, it_block_ends

ELF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "elf", "dil2_O0.elf")
CODE = FLASH.base + 0x300000


def program(halfwords):
    m = Machine(ELF)
    m.uc.mem_write(CODE, struct.pack(f"<{len(halfwords)}H", *halfwords))
    m._pristine["flash"] = m.read(FLASH.base, FLASH.size)
    m.image._by_name["t"] = (CODE, 2 * len(halfwords), "STT_FUNC")
    return m


def run(m, args, off=None, it_end=None):
    skip = Skip(m, CODE + off, it_end and CODE + it_end) if off is not None else None
    m.restore_memory()
    r = m.call("t", args)
    fired = skip.fired if skip else 0
    if skip:
        skip.detach()
    return r, fired


# cmp r0,#0; ite eq; moveq r1,#1; movne r1,#2; mov r0,r1; bx lr
ite = [0x2800, 0xBF0C, 0x2101, 0x2102, 0x4608, 0x4770]
m = program(ite)
insns = []
pc = CODE
while pc < CODE + 12:
    a, n, asm = m.disasm_one(pc)
    insns.append((a, n, asm))
    pc += n
assert it_block_ends(insns) == {CODE + 4: CODE + 8, CODE + 6: CODE + 8}, it_block_ends(insns)
assert run(m, [0, 9]) == (1, 0) and run(m, [1, 9]) == (2, 0)
assert run(m, [0, 9], 4, 8) == (9, 1)       # moveq skipped
assert run(m, [1, 9], 6, 8) == (9, 1)       # movne skipped
assert run(m, [1, 9], 4, 8) == (2, 0)       # moveq not executed anyway
assert run(m, [0, 9]) == (1, 0) and run(m, [1, 9]) == (2, 0)  # bytes are back
assert run(m, [0, 9], 8) == (0, 1)          # mov r0,r1 outside the block

# only the first execution: r0 = 0; 3 x { cmp r1,#0; it ne; addne r0,#1; subs r2,#1; bne }
loop = program([0x2000, 0x2900, 0xBF18, 0x3001, 0x3A01, 0xD1FA, 0x4770])
assert run(loop, [0, 1, 3]) == (3, 0)
assert run(loop, [0, 1, 3], 6, 8) == (2, 1)

# a crash inside an IT block must not make the next call conditional
# cmp r0,#0; itttt ne; ldrne r3,[r1]; movne r0,#1 x3; bx lr   /   5 x adds r0,#1; bx lr
crash = [0x2800, 0xBF1F, 0x680B, 0x2001, 0x2001, 0x2001, 0x4770]
m = program(crash + [0x3001] * 5 + [0x4770])
m.image._by_name["u"] = (CODE + 2 * len(crash), 12, "STT_FUNC")
try:
    m.call("t", [1, 0x60000000])
    raise AssertionError("expected a crash")
except EmulationError:
    pass
m.restore_memory()
assert m.call("u", [0]) == 5

# stray writes anywhere are undone
m.write(0x203FFFF0, b"dirty")
m.write(0x20000010, b"dirty")
m.write(0x3003FFF0, b"dirty")
m.write(0x00100000, b"dirty")
m.restore_memory()
assert not any(any(m.read(a, 5)) for a in (0x203FFFF0, 0x20000010, 0x3003FFF0, 0x00100000))
print("ok")
