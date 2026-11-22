"""LLVM IR text helpers: instruction indexing, debug relabelling, PC -> IR.

Instruction index = the flat per-function counter of Dilithium-LLVM's taint
pass (level_prop.cpp): basic blocks in order, every instruction, debug
records (#dbg_*) not counted.  It is the `_line<N>` of the fault report.

relabel() gives instruction N of every function its own debug location
`line N+1`; an ELF compiled from relabelled IR then maps every PC back to
(function, IR index) exactly through its DWARF line table (pc_to_ir).

    python ir.py relabel in.ll out.ll
"""
import os
import re
import sys
from dataclasses import dataclass, field

from elftools.elf.elffile import ELFFile

DEFINE = re.compile(r"^define\b.*?@(\"[^\"]+\"|[\w.$-]+)\(.*?(?:!dbg !(\d+))?\s*\{\s*$")
LABEL = re.compile(r"^(\"[^\"]+\"|[\w.$-]+):")
DBG = re.compile(r", !dbg !(\d+)")
MD = re.compile(r"^!(\d+) = (?:distinct )?!(\w+)\((.*)\)\s*$")
FIELD = re.compile(r"(\w+): (\"(?:[^\"\\]|\\.)*\"|![\d]+|[^,]+)")


@dataclass
class Inst:
    index: int
    lineno: int  # 0-based line in the .ll text
    text: str  # without the !dbg attachment
    dbg: int | None


@dataclass
class Function:
    name: str
    subprogram: int | None
    lineno: int
    params: list = field(default_factory=list)  # [(ir_type, name)]
    insts: list = field(default_factory=list)


def parse_params(define_line):
    """`define ... @f(ptr noundef %a, i16 zeroext %n)` -> [("ptr","a"), ("i16","n")]."""
    start = define_line.index("(", define_line.index("@"))
    depth = 0
    for i in range(start, len(define_line)):
        depth += define_line[i] == "("
        depth -= define_line[i] == ")"
        if depth == 0:
            break
    out, depth, cur = [], 0, ""
    for ch in define_line[start + 1:i] + ",":
        if ch == "," and depth == 0:
            cur = cur.strip()
            if cur:
                m = re.search(r"%([\w.]+)$", cur)
                out.append((cur.split()[0], m.group(1) if m else f"arg{len(out)}"))
            cur = ""
            continue
        depth += ch in "([{<"
        depth -= ch in ")]}>"
        cur += ch
    return out


class Module:
    def __init__(self, text):
        self.text = text
        self.lines = text.split("\n")
        self.functions, self.md, self.max_md_id = {}, {}, 0
        fn = None
        for lineno, line in enumerate(self.lines):
            if fn is None:
                m = DEFINE.match(line)
                if m:
                    fn = Function(m.group(1).strip('"'), int(m.group(2)) if m.group(2) else None,
                                  lineno, parse_params(line))
                    continue
                m = MD.match(line)
                if m:
                    mid = int(m.group(1))
                    self.max_md_id = max(self.max_md_id, mid)
                    self.md[mid] = (m.group(2), dict(FIELD.findall(m.group(3))))
                elif line.startswith("!") and line.split(" ", 1)[0][1:].isdigit():
                    self.max_md_id = max(self.max_md_id, int(line.split(" ", 1)[0][1:]))
                continue
            if line.startswith("}"):
                self.functions[fn.name] = fn
                fn = None
                continue
            s = line.strip()
            if not s or s.startswith(";") or s.startswith("#dbg_") or LABEL.match(line):
                continue
            d = DBG.search(line)
            fn.insts.append(Inst(len(fn.insts), lineno, DBG.sub("", line).rstrip(),
                                 int(d.group(1)) if d else None))

    def _ref(self, v):
        return int(v[1:]) if v and v.startswith("!") and v[1:].isdigit() else None

    def loc_scope(self, md_id):
        return self._ref(self.md[md_id][1].get("scope"))

    def location(self, md_id):
        """DILocation id -> (file basename, line, column)."""
        f = self.md[md_id][1]
        return self.scope_file(self._ref(f.get("scope"))), int(f.get("line", 0)), int(f.get("column", 0))

    def scope_file(self, scope):
        for _ in range(64):
            if scope is None:
                break
            kind, f = self.md[scope]
            if kind == "DIFile":
                return os.path.basename(f.get("filename", "").strip('"'))
            ref = self._ref(f.get("file"))
            if ref is not None:
                return os.path.basename(self.md[ref][1].get("filename", "").strip('"'))
            scope = self._ref(f.get("scope"))
        return ""

    def is_relabelled(self):
        for fn in self.functions.values():
            for inst in fn.insts:
                if inst.dbg is not None and self.location(inst.dbg)[1] != inst.index + 1:
                    return False
        return True


def parse_module(text):
    return Module(text)


def relabel(text):
    """Every instruction gets `!DILocation(line: index+1, column: 1, scope: <its scope>)`."""
    mod = Module(text)
    lines = mod.lines
    next_id, new_md = mod.max_md_id + 1, []
    for fn in mod.functions.values():
        for inst in fn.insts:
            src = lines[inst.lineno]
            m = DBG.search(src)
            scope = mod.loc_scope(int(m.group(1))) if m else fn.subprogram
            if scope is None:
                continue
            new_md.append(f"!{next_id} = !DILocation(line: {inst.index + 1}, column: 1, scope: !{scope})")
            tag = f", !dbg !{next_id}"
            if m:
                src = src[:m.start()] + tag + src[m.end():]
            else:
                k = src.find(", !")
                src = src[:k] + tag + src[k:] if k >= 0 else src + tag
            lines[inst.lineno] = src
            next_id += 1
    return "\n".join(lines).rstrip("\n") + "\n" + "\n".join(new_md) + "\n"


# --- DWARF ------------------------------------------------------------------------

def _line_rows(elf):
    """[(lo, hi, file_basename, line, column)] from every CU's line program."""
    rows = []
    dw = elf.get_dwarf_info()
    for cu in dw.iter_CUs():
        lp = dw.line_program_for_CU(cu)
        if lp is None:
            continue
        files, ver, prev = lp.header.file_entry, lp.header.version, None
        for ent in lp.get_entries():
            st = ent.state
            if st is None:
                continue
            if prev is not None and st.address > prev.address:
                fidx = prev.file if ver >= 5 else prev.file - 1
                name = files[fidx].name.decode() if 0 <= fidx < len(files) else ""
                rows.append((prev.address, st.address, os.path.basename(name), prev.line, prev.column))
            prev = None if st.end_sequence else st
    return rows


def _die_ranges(dw, cu, die):
    lo = die.attributes.get("DW_AT_low_pc")
    hi = die.attributes.get("DW_AT_high_pc")
    if lo is not None and hi is not None:
        h = hi.value if hi.form == "DW_FORM_addr" else lo.value + hi.value
        return [(lo.value, h)]
    if "DW_AT_ranges" in die.attributes:
        base = cu.get_top_DIE().attributes.get("DW_AT_low_pc")
        base = base.value if base else 0
        rl = dw.range_lists()
        out = []
        for r in rl.get_range_list_at_offset(die.attributes["DW_AT_ranges"].value, cu=cu):
            if hasattr(r, "begin_offset"):
                out.append((base + r.begin_offset, base + r.end_offset))
        return out
    return []


def _origin_name(die):
    seen = 0
    while die is not None and seen < 8:
        for k in ("DW_AT_linkage_name", "DW_AT_name"):
            if k in die.attributes:
                return die.attributes[k].value.decode()
        if "DW_AT_abstract_origin" in die.attributes:
            die = die.get_DIE_from_attribute("DW_AT_abstract_origin")
        elif "DW_AT_specification" in die.attributes:
            die = die.get_DIE_from_attribute("DW_AT_specification")
        else:
            break
        seen += 1
    return None


def _inlined_ranges(elf):
    """[(lo, hi, depth, callee_name)] for every DW_TAG_inlined_subroutine."""
    out = []
    dw = elf.get_dwarf_info()
    for cu in dw.iter_CUs():
        stack = [(cu.get_top_DIE(), 0)]
        while stack:
            die, depth = stack.pop()
            d = depth
            if die.tag == "DW_TAG_inlined_subroutine":
                d = depth + 1
                name = _origin_name(die)
                for lo, hi in _die_ranges(dw, cu, die):
                    out.append((lo, hi, d, name))
            for child in die.iter_children():
                stack.append((child, d))
    return out


class PcToIR:
    """PC -> [(ir_function, ir_index, match)] through the DWARF line table.

    match: exact (relabelled build: line-1 is the index), loc (same file/line/
    column as the IR instruction's DILocation; possibly several), line (line
    only), none (no line-table row, e.g. prologue or literal pool)."""

    def __init__(self, module, elf_path):
        self.mod = module
        with open(elf_path, "rb") as fh:
            elf = ELFFile(fh)
            if not elf.has_dwarf_info():
                raise SystemExit(f"{elf_path}: no DWARF line info")
            self.rows = sorted(_line_rows(elf))
            self.inlined = _inlined_ranges(elf)
        self._starts = [r[0] for r in self.rows]
        self.exact = module.is_relabelled()
        self._loc_index = {}

    def _row(self, pc):
        import bisect
        i = bisect.bisect_right(self._starts, pc) - 1
        while i >= 0 and self.rows[i][0] <= pc:
            if pc < self.rows[i][1]:
                return self.rows[i]
            i -= 1
        return None

    def owner(self, pc, swept):
        """The function whose code this PC is: the innermost inlined callee, else `swept`."""
        best = None
        for lo, hi, depth, name in self.inlined:
            if lo <= pc < hi and name and (best is None or depth > best[0]):
                best = (depth, name)
        return best[1] if best else swept

    def lookup(self, pc, swept):
        fn_name = self.owner(pc, swept)
        row = self._row(pc)
        fn = self.mod.functions.get(fn_name)
        if row is None or row[3] == 0 or fn is None:
            return fn_name, [], "none"
        _, _, fname, line, col = row
        if self.exact:
            idx = line - 1
            return fn_name, ([idx] if 0 <= idx < len(fn.insts) else []), ("exact" if 0 <= idx < len(fn.insts) else "none")
        key = fn_name
        if key not in self._loc_index:
            idx = {}
            for inst in fn.insts:
                if inst.dbg is not None:
                    idx.setdefault(self.mod.location(inst.dbg), []).append(inst.index)
            self._loc_index[key] = idx
        idx = self._loc_index[key]
        hit = idx.get((fname, line, col))
        if hit:
            return fn_name, hit, "loc"
        hit = sorted(i for (f, l, _), v in idx.items() if f == fname and l == line for i in v)
        return fn_name, hit, ("line" if hit else "none")


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "relabel":
        sys.exit(__doc__)
    open(sys.argv[3], "w").write(relabel(open(sys.argv[2]).read()))
