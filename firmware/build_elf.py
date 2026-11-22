#!/usr/bin/env python3
"""Optional: compile an LLVM IR file (or link any .o) into an MPS2-AN386 ELF.

The fault sweep only needs an ARM ELF with DWARF line info and the same
function names, from any compiler.  This helper lowers the exact IR the
IR-level study faulted, in-process with llvmlite (LLVM >= 20, ARM backend):

  -O 0       codegen only, no IR optimisation
  -O 1..3    the LLVM new-pass-manager default<O1..3> pipeline (inlining,
             unrolling, vectorisation, ...) followed by codegen at that level
  -O s       size: every function gets the `optsize` attribute (as clang -Os
             emits), then the O2 pipeline with loop unrolling and vectorisation
             off and the -Os inline threshold (50), and O2 codegen.  llvmlite
             exposes no size level, so this approximates default<Os>.  (LLVM 20's
             own opt -passes='default<Os>' + llc -O2 on the same IR gives 2816 B
             for the 38 config functions, this 2812 B.)
  --relabel  first give every IR instruction its own debug line (ir.relabel), so
             correlate.py maps PCs back to IR instruction indices exactly;
             writes <out>.relabel.ll (pass it as --ir).  The .text is checked to
             be identical to a build without relabelling.

  python firmware/build_elf.py data/dilithium2.ll -O 0 --relabel -o build/dil2_O0.elf
  python firmware/build_elf.py data/dilithium2.ll -O 3 --relabel -o build/dil2_O3.elf
  python firmware/build_elf.py data/dilithium2.ll -O s --relabel -o build/dil2_Os.elf
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
ARCH = ["-mcpu=cortex-m4", "-mthumb", "-mfloat-abi=hard", "-mfpu=fpv4-sp-d16"]


def add_optsize(text):
    """Add `optsize` to every attribute group (what clang -Os puts on functions)."""
    import re
    return re.sub(r"^(attributes #\d+ = \{)", r"\1 optsize", text, flags=re.M)


def compile_ll(text, obj, opt):
    import llvmlite.binding as llvm

    size = opt == "s"
    if size:
        text, opt = add_optsize(text), 2

    llvm.initialize_all_targets()
    llvm.initialize_all_asmprinters()
    mod = llvm.parse_assembly(text)
    mod.verify()
    tm = llvm.Target.from_triple(mod.triple).create_target_machine(
        cpu="cortex-m4", features="+vfp4d16sp,+dsp,+fp16,+hwdiv,+thumb-mode",
        opt=opt, reloc="static", codemodel="small")
    if opt > 0:
        pto = llvm.create_pipeline_tuning_options(speed_level=opt)
        if size:
            pto.loop_unrolling = False
            pto.loop_vectorization = False
            pto.slp_vectorization = False
            pto.inlining_threshold = 50
        pb = llvm.create_pass_builder(tm, pto)
        pb.getModulePassManager().run(mod, pb)
        mod.verify()
    open(obj, "wb").write(tm.emit_object(mod))


def link(obj, out, stem):
    startup = stem + ".startup.o"
    subprocess.run(["arm-none-eabi-gcc", *ARCH, "-O1", "-c", os.path.join(HERE, "startup.c"), "-o", startup], check=True)
    subprocess.run(["arm-none-eabi-gcc", *ARCH, "-nostartfiles", "--specs=nosys.specs", "-T",
                    os.path.join(HERE, "mps2.ld"), startup, obj, "-o", out,
                    "-Wl,--no-warn-rwx-segments,--no-enum-size-warning"],
                   stderr=subprocess.DEVNULL, check=True)


def text_section(elf):
    return subprocess.run(["arm-none-eabi-objcopy", "-O", "binary", "--only-section=.text", elf, "/dev/stdout"],
                          check=True, capture_output=True).stdout


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help=".ll or .o")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("-O", "--opt", default="0", choices=["0", "1", "2", "3", "s"])
    ap.add_argument("--relabel", action="store_true")
    a = ap.parse_args()
    a.opt = a.opt if a.opt == "s" else int(a.opt)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    stem = os.path.splitext(a.out)[0]
    if not a.input.endswith(".ll"):
        link(a.input, a.out, stem)
        print(a.out)
        return
    text = open(a.input).read()
    if a.relabel:
        import ir

        plain = stem + ".plain.elf"
        compile_ll(text, stem + ".plain.o", a.opt)
        link(stem + ".plain.o", plain, stem + ".plain")
        text = ir.relabel(text)
        open(stem + ".relabel.ll", "w").write(text)
    compile_ll(text, stem + ".o", a.opt)
    link(stem + ".o", a.out, stem)
    if a.relabel:
        same = text_section(plain) == text_section(a.out)
        print(f"{stem}.relabel.ll written; .text identical to the unrelabelled build: {same}")
        if not same:
            print("[!] relabelling changed code generation: PC -> IR mapping is approximate", file=sys.stderr)
    print(a.out)


if __name__ == "__main__":
    main()
