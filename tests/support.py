"""Shared by the tests: import path and small hand-assembled Thumb programs."""
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from emu import FLASH, Machine, Skip  # noqa: E402

ELF = os.path.join(ROOT, "elf", "dil2_O0.elf")
CODE = FLASH.base + 0x300000  # unused flash


def program(**functions):
    """A Machine with each function's halfwords placed one after another."""
    m, addr = Machine(ELF), CODE
    for name, halfwords in functions.items():
        m.add_function(name, addr, struct.pack(f"<{len(halfwords)}H", *halfwords))
        addr += 2 * len(halfwords)
    return m


def run(m, func, args, skip_at=None, it_block=None):
    """(return value, times the skip fired); offsets are relative to CODE."""
    skip = None if skip_at is None else Skip(m, CODE + skip_at, it_block and tuple(CODE + o for o in it_block))
    m.restore_memory()
    r0 = m.call(func, args)
    if skip is None:
        return r0, 0
    skip.detach()
    return r0, skip.fired
