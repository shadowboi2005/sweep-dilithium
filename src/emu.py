"""Cortex-M4 (MPS2-AN386) emulator on Unicorn, plus the instruction-skip fault.

Trimmed from ucpqc (machine.py / elfimage.py / platform.py) to what the fault
sweep calls: load an ELF, call one function with the AAPCS convention, hook
a single PC, and skip the instruction there on its first execution.
"""
import bisect
import ctypes
import hashlib
import mmap
import os
import re
import struct
import time
from dataclasses import dataclass

import capstone
import unicorn
from elftools.elf.elffile import ELFFile
from unicorn import Uc, UcError
from unicorn import arm_const as A

REGS = {**{f"r{i}": getattr(A, f"UC_ARM_REG_R{i}") for i in range(13)},
        "sp": A.UC_ARM_REG_SP, "lr": A.UC_ARM_REG_LR, "pc": A.UC_ARM_REG_PC}
_libc = ctypes.CDLL(None)
EXCP_BKPT = 7
SYS_EXIT = 0x18


# --- board ---------------------------------------------------------------------

@dataclass(frozen=True)
class Region:
    name: str
    base: int
    size: int

    @property
    def end(self):
        return self.base + self.size - 1

    def contains(self, addr):
        return self.base <= addr <= self.end


FLASH = Region("flash", 0x00000000, 0x00400000)       # code, rodata, data, bss
SRAM = Region("sram", 0x20000000, 0x00400000)          # stack grows down from the top
APB = Region("apb", 0x40000000, 0x00100000)            # CMSDK peripherals (MMIO)
PPB = Region("ppb", 0xE0000000, 0x00100000)            # SysTick / SCB / DWT (MMIO)
SCRATCH = Region("scratch", 0x30000000, 0x00100000)    # argument buffers for direct calls
TRAMPOLINE = Region("trampoline", 0x1F000000, 0x1000)  # return address of direct calls
UART0 = 0x40004000
SYSTICK_LOAD, SYSTICK_VAL, SYSTICK_CTRL = 0xE000E014, 0xE000E018, 0xE000E010
DWT_CYCCNT, SCB_CPUID = 0xE0001004, 0xE000ED00


class Peripherals:
    """MMIO model for what Unicorn does not emulate (UART, SysTick, DWT, SCB).
    Unmodelled registers behave as plain word stores."""

    def __init__(self, cycles):
        self._cycles = cycles
        self.regs = {}
        self.uart_out = bytearray()
        self._reload = 0x00FFFFFF

    def read(self, addr, size):
        if UART0 <= addr < UART0 + 0x1000:
            return 0 if addr - UART0 == 4 else self.regs.get(addr, 0)
        if addr == SYSTICK_VAL:
            period = self._reload + 1
            return (period - 1) - (self._cycles() % period)
        if addr == SYSTICK_LOAD:
            return self._reload
        if addr == DWT_CYCCNT:
            return self._cycles() & 0xFFFFFFFF
        if addr == SCB_CPUID:
            return 0x410FC241
        return self.regs.get(addr, 0)

    def write(self, addr, size, value):
        if addr == UART0:
            self.uart_out.append(value & 0xFF)
        elif addr == SYSTICK_LOAD:
            self._reload = value & 0x00FFFFFF
        else:
            self.regs[addr] = value


# --- ELF -------------------------------------------------------------------------

@dataclass(frozen=True)
class Segment:
    addr: int
    data: bytes
    memsz: int


class ElfImage:
    """Loadable segments plus the function symbol index of an ARM ELF."""

    def __init__(self, path):
        self.path = str(path)
        self.segments, syms = [], []
        with open(self.path, "rb") as fh:
            elf = ELFFile(fh)
            for seg in elf.iter_segments():
                if seg["p_type"] == "PT_LOAD" and seg["p_memsz"]:
                    self.segments.append(Segment(seg["p_paddr"], seg.data(), seg["p_memsz"]))
            for sec in elf.iter_sections():
                if sec.header["sh_type"] != "SHT_SYMTAB":
                    continue
                for s in sec.iter_symbols():
                    kind = s["st_info"]["type"]
                    if s.name and kind in ("STT_FUNC", "STT_OBJECT", "STT_NOTYPE"):
                        addr = s["st_value"] & ~1 if kind == "STT_FUNC" else s["st_value"]
                        syms.append((s.name, addr, s["st_size"], kind))
        self._by_name = {}
        for name, addr, size, kind in syms:
            if name not in self._by_name or (kind == "STT_FUNC" and self._by_name[name][2] != "STT_FUNC"):
                self._by_name[name] = (addr, size, kind)
        funcs, seen = [], set()
        for name, addr, size, kind in sorted((s for s in syms if s[3] == "STT_FUNC"), key=lambda s: (s[1], -s[2])):
            if addr not in seen:
                seen.add(addr)
                funcs.append((addr, size, name))
        self._starts = [f[0] for f in funcs]
        self._ends = [a + s if s else (funcs[i + 1][0] if i + 1 < len(funcs) else a + 4)
                      for i, (a, s, _) in enumerate(funcs)]
        self._names = [f[2] for f in funcs]

    def addr_of(self, name_or_addr):
        if isinstance(name_or_addr, int):
            return name_or_addr
        try:
            return self._by_name[name_or_addr][0]
        except KeyError:
            raise KeyError(f"{self.path}: no symbol {name_or_addr!r}") from None

    def has(self, name):
        return name in self._by_name

    def extent_of(self, name_or_addr):
        """(start, end) of the function containing the address / named function."""
        addr = self.addr_of(name_or_addr)
        i = bisect.bisect_right(self._starts, addr) - 1
        if i < 0 or addr >= self._ends[i]:
            raise KeyError(f"{addr:#x} is not inside a function")
        return self._starts[i], self._ends[i]

    def describe(self, addr):
        i = bisect.bisect_right(self._starts, addr) - 1
        if i < 0 or addr >= self._ends[i]:
            return f"{addr:#x}"
        off = addr - self._starts[i]
        return f"{self._names[i]}+{off:#x}" if off else self._names[i]


# --- machine ---------------------------------------------------------------------

class EmulationError(RuntimeError):
    """The guest faulted (bad access, CPU exception)."""


class EmulationTimeout(RuntimeError):
    """The guest did not return within the wall-clock / instruction cap."""


class Machine:
    """A Cortex-M4 running one firmware image; functions are called directly
    (no boot): arguments go into scratch memory, the return address is a
    trampoline page that emulation stops at."""

    def __init__(self, path):
        self.image = ElfImage(path)
        self.icount = 0
        self._block_cache = {}
        self._cpu0 = None
        self._skip = None  # the armed Skip, if any
        self._stray = False
        self._halt = None
        self.uc = Uc(unicorn.UC_ARCH_ARM, unicorn.UC_MODE_THUMB | unicorn.UC_MODE_MCLASS)
        self.uc.ctl_set_cpu_model(A.UC_CPU_ARM_CORTEX_M4)
        self.cs = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)
        self.peripherals = Peripherals(lambda: self.icount)
        # Private copy-on-write mappings: discarding a trial's pages brings back
        # the pristine contents at a cost proportional to the pages it touched.
        self._maps = {}
        for r in (FLASH, SRAM, SCRATCH, TRAMPOLINE):
            fd = os.memfd_create(r.name)
            os.ftruncate(fd, r.size)
            mm = mmap.mmap(fd, r.size, mmap.MAP_PRIVATE, mmap.PROT_READ | mmap.PROT_WRITE)
            host = ctypes.addressof(ctypes.c_char.from_buffer(mm))
            self._maps[r.name] = (fd, mm, host, r)
            self.uc.mem_map_ptr(r.base, r.size, unicorn.UC_PROT_ALL, host)
        self._code = []  # (host address, bytes) of the loaded segments
        self._install(TRAMPOLINE.base, b"\xfe\xe7" * 8)  # b .
        for seg in self.image.segments:
            self._install(seg.addr, seg.data)
        self._code_lo = min(x.addr for x in self.image.segments)
        self._code_hi = max(x.addr + len(x.data) for x in self.image.segments)
        for r in (APB, PPB):
            self.uc.mmio_map(r.base, r.size, self._mmio_read, r.base, self._mmio_write, r.base)
        self.uc.hook_add(unicorn.UC_HOOK_INTR, self._hook_intr)
        self.uc.hook_add(unicorn.UC_HOOK_BLOCK, self._hook_block)
        self.uc.hook_add(unicorn.UC_HOOK_MEM_READ_UNMAPPED | unicorn.UC_HOOK_MEM_WRITE_UNMAPPED
                         | unicorn.UC_HOOK_MEM_FETCH_UNMAPPED, self._hook_unmapped)
        # Enable the FPU (CPACR CP10/CP11), as the reset handler would.
        self.peripherals.regs[0xE000ED88] = 0xF << 20
        self._alloc_ptr = SCRATCH.base
        self._regs0 = dict(self.peripherals.regs)

    def _install(self, addr, data):
        """Put `data` into the pristine contents of memory."""
        for fd, mm, host, r in self._maps.values():
            if r.contains(addr):
                os.pwrite(fd, data, addr - r.base)
                if r is FLASH:
                    self._code.append((host + addr - r.base, bytes(data)))
                return
        raise ValueError(f"{addr:#x} is not in memory")

    def restore_memory(self):
        """Put every mapped byte (code, data, .bss, stack, scratch, return page)
        and the peripherals back to their state before the first call."""
        for host, data in self._code:
            if _libc.memcmp(ctypes.c_void_p(host), data, ctypes.c_size_t(len(data))):
                self._stray = True
        for _, mm, _, _ in self._maps.values():
            mm.madvise(mmap.MADV_DONTNEED)
        if self._stray:  # code changed, or a trial ran data as code: drop its translations
            self.uc.ctl_flush_tb()
            self._stray = False
        self.peripherals.regs = dict(self._regs0)
        self.peripherals._reload = 0x00FFFFFF
        self.peripherals.uart_out.clear()

    def _mmio_read(self, uc, offset, size, base):
        return self.peripherals.read(base + offset, size)

    def _mmio_write(self, uc, offset, size, value, base):
        self.peripherals.write(base + offset, size, value)

    def _hook_unmapped(self, uc, access, addr, size, value, _):
        self._halt = f"unmapped access to {addr:#010x}"
        return False

    def _hook_block(self, uc, addr, size, _):
        if not (self._code_lo <= addr < self._code_hi or addr == TRAMPOLINE.base):
            self._stray = True
        if size:
            n = self._block_cache.get((addr, size))
            if n is None:
                n = sum(1 for _ in self.cs.disasm(bytes(uc.mem_read(addr, size)), addr))
                self._block_cache[(addr, size)] = n
            self.icount += n

    def _hook_intr(self, uc, intno, _):
        pc = uc.reg_read(A.UC_ARM_REG_PC)
        if intno == EXCP_BKPT and struct.unpack("<H", uc.mem_read(pc, 2))[0] == 0xBEAB:
            if uc.reg_read(A.UC_ARM_REG_R0) == SYS_EXIT:
                self._halt = "semihosting exit"
                uc.emu_stop()
                return
            uc.reg_write(A.UC_ARM_REG_R0, 0)  # other semihosting calls: ignored
            uc.reg_write(A.UC_ARM_REG_PC, pc + 2)
            return
        self._halt = f"cpu exception {intno} at {pc:#x}"
        uc.emu_stop()

    # memory / registers
    def reg(self, name):
        return self.uc.reg_read(REGS[name])

    def set_reg(self, name, value):
        self.uc.reg_write(REGS[name], value & 0xFFFFFFFF)

    def read(self, addr, size):
        return bytes(self.uc.mem_read(addr, size))

    def write(self, addr, data):
        self.uc.mem_write(addr, bytes(data))

    def alloc(self, data, align=16):
        """Copy `data` into scratch memory and return its address."""
        base = (self._alloc_ptr + align - 1) & ~(align - 1)
        if base + len(data) > SCRATCH.base + SCRATCH.size:
            raise EmulationError("scratch memory exhausted")
        self._alloc_ptr = base + max(len(data), 1)
        if data:
            self.uc.mem_write(base, bytes(data))
        return base

    def scratch_reset(self):
        self._alloc_ptr = SCRATCH.base

    def addr_of(self, name):
        return self.image.addr_of(name)

    # hooks
    def hook_code(self, callback, begin, end):
        def shim(uc, address, size, _):
            callback(self, address, size)
        h = self.uc.hook_add(unicorn.UC_HOOK_CODE, shim, None, begin, end)
        self.uc.ctl_flush_tb()  # hooks only apply to blocks translated afterwards
        return h

    def unhook(self, handle):
        self.uc.hook_del(handle)
        self.uc.ctl_flush_tb()

    def stub_randombytes(self, seed=b"targeted_unicorn"):
        """Replace the guest RNG (if linked) with a SHAKE256 stream; returns it."""
        for sym in ("randombytes", "dilithium_llvm_randombytes"):
            if self.image.has(sym):
                break
        else:
            return None
        stream = _Xof(seed)

        def handler(m, address, size):
            buf, n = m.reg("r0"), m.reg("r1")
            m.write(buf, stream.read(n))
            m.set_reg("r0", 0)
            m.set_reg("pc", m.reg("lr") | 1)

        addr = self.addr_of(sym)
        self.hook_code(handler, addr, addr)
        return stream

    # execution
    def call(self, func, args=(), max_instructions=0, timeout_s=0.0, stack_size=0x40000):
        """AAPCS call of `func`; returns r0.  Raises EmulationError on a guest
        fault and EmulationTimeout if it does not return within the caps."""
        # Every call starts from the CPU state before the first one (registers,
        # flags, IT state, VFP), so a skipped initialisation never sees values
        # left by an earlier trial on this machine.
        if self._cpu0 is None:
            self._cpu0 = self.uc.context_save()
        else:
            self.uc.context_restore(self._cpu0)
        addr = self.addr_of(func)
        ret = TRAMPOLINE.base
        sp = (SRAM.base + SRAM.size - stack_size) & ~7
        args = [a & 0xFFFFFFFF for a in args]
        stacked = args[4:]
        if stacked:
            if len(stacked) % 2:
                stacked.append(0)
            sp -= 4 * len(stacked)
            self.write(sp, struct.pack(f"<{len(stacked)}I", *stacked))
        for i, v in enumerate(args[:4]):
            self.set_reg(f"r{i}", v)
        self.set_reg("sp", sp)
        self.set_reg("lr", ret | 1)
        self._halt = None
        t_end = time.perf_counter() + timeout_s
        skip = self._skip
        try:
            if skip is not None:
                skip.begin()
            self.uc.emu_start(addr | 1, ret, timeout=int(timeout_s * 1e6), count=max_instructions)
            if skip is not None and skip.resume_at is not None and self._halt is None:
                # an IT-block member was replaced by a NOP for one execution: put it back and go on
                at = skip.resume_at
                skip.end()
                left = max(t_end - time.perf_counter(), 1e-3) if timeout_s else 0
                self.uc.emu_start(at | 1, ret, timeout=int(left * 1e6), count=max_instructions)
        except UcError as exc:
            raise EmulationError(f"{self._halt or exc} at {self.image.describe(self.reg('pc'))}") from None
        finally:
            if skip is not None:
                skip.end()
        if self._halt is not None:
            raise EmulationError(self._halt)
        if self.reg("pc") & ~1 != ret:
            raise EmulationTimeout(f"{func} did not return (pc={self.image.describe(self.reg('pc') & ~1)})")
        return self.reg("r0")

    def disasm_one(self, addr):
        for insn in self.cs.disasm(self.read(addr, 8), addr, 1):
            return insn.address, insn.size, f"{insn.mnemonic} {insn.op_str}".strip()
        return addr, 2, "(undecodable)"

    def function_insns(self, func):
        """Linear disassembly of a function's symbol range: [(pc, size, asm)]."""
        pc, end = self.image.extent_of(func)
        out = []
        while pc < end:
            a, size, asm = self.disasm_one(pc)
            out.append((pc, size, asm))
            pc += size or 2
        return out


class Skip:
    """Instruction-skip fault: the instruction at `pc` is not executed the
    first time it would be (hit=1); later executions run normally.  Call
    reset() before every faulted trial, disarm() for an unfaulted one.

    Outside IT blocks the hook moves PC past the instruction.  Unicorn ignores
    that for an instruction inside an IT block, so there the bytes are a NOP
    (under the same condition) until it has executed once; the machine then
    stops at the end of the block, puts the bytes back and resumes."""

    def __init__(self, machine, pc, it_end=None):
        self.machine, self.pc, self.it_end = machine, pc, it_end
        self.size = machine.disasm_one(pc)[1] or 2
        self.hits = self.fired = 0
        self.resume_at = self._orig = None
        self._handles = [machine.hook_code(self._on_pc, pc, pc)]
        if it_end is not None:
            self._handles.append(machine.hook_code(self._on_it_end, it_end, it_end))
        machine._skip = self

    def reset(self):
        self.hits = self.fired = 0

    def disarm(self):
        self.hits = 1

    def begin(self):
        self.resume_at = None
        if self.it_end is not None and self.hits == 0:
            self._orig = self.machine.read(self.pc, self.size)
            self.machine.write(self.pc, b"\x00\xbf" if self.size == 2 else b"\xaf\xf3\x00\x80")
            self.machine.uc.ctl_flush_tb()

    def end(self):
        if self._orig is not None:
            self.machine.write(self.pc, self._orig)
            self.machine.uc.ctl_flush_tb()
            self._orig = None

    def _on_pc(self, m, addr, size):
        self.hits += 1
        if self.hits == 1:
            self.fired += 1
            if self.it_end is None:
                m.set_reg("pc", (addr + self.size) | 1)

    def _on_it_end(self, m, addr, size):
        if self._orig is not None and self.fired:
            self.resume_at = addr
            m.uc.emu_stop()

    def detach(self):
        self.end()
        for h in self._handles:
            self.machine.unhook(h)
        self.machine._skip = None


def it_block_ends(insns):
    """{pc: address after the IT block} for every member of an IT block in
    function_insns() output."""
    ends = {}
    for i, (_, _, asm) in enumerate(insns):
        op = asm.split()[0]
        if re.fullmatch(r"it[te]{0,3}", op):
            block = insns[i + 1:i + len(op)]
            for pc, _, _ in block:
                ends[pc] = block[-1][0] + block[-1][1]
    return ends


class _Xof:
    def __init__(self, seed):
        self.seed, self.pos = bytes(seed), 0

    def read(self, n):
        out = hashlib.shake_256(self.seed).digest(self.pos + n)[self.pos:]
        self.pos += n
        return out

    def reset(self):
        self.pos = 0
