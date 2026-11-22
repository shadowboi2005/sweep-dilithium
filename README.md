# Do LLVM-IR fault findings survive as machine-instruction skips?

> **Status: the CSVs in `results/` are out of date.** They come from the code before the fixes that followed `reviews/REVIEW_CODE.md` (state leaking between trials, skips inside IT blocks having no effect, correlation counting copies inlined into other functions). Don't quote them; a re-run is pending, and `REPORT.md` will follow it.

The IR-level study (Dilithium-LLVM + QEMU-test) mutates single LLVM IR instructions and scores each mutant with three tests:
- **ineffective**: whether the fault is ineffective for some secret values and effective for others;
- **correction**: whether there is a constant output difference α≠0 for some secret;
- **direct_leak**: whether the faulted output copies the secret.

The verdicts are in `fault_report_dilithium.csv` (`scheme,function,fault,source,test,detected,detail`).

This tool asks the same question at machine level, in two steps, with **no IR-fault information** used to find the faults.

1. **Find** (`find_faults.py`). Every machine instruction of each target function in a compiled Cortex-M4 ELF is skipped on its first execution in a trial (a Unicorn code hook; hit=1). Each skip is scored with the same three tests, on the same inputs and with the same stopping rule as the IR-level run.
2. **Correlate** (`correlate.py`). Each PC is mapped to its IR instruction through the DWARF line table, then joined with the report rows for that IR instruction.

## Same experiment as the IR-level run
Everything except the fault itself is ported from QEMU-test (pinned `beaf3cc`, file names are in the module docstrings):

| | |
|---|---|
| backgrounds p | public seeds `--seeds 0 1 2 3` (`random.Random(p)`), each buffer drawn from its trace distribution |
| ephemeral secrets r | `--eph-draws 4` draws `Random("eph:j")`, crossed with every p (functions with `eph_secret`) |
| secret | `secret-buf` at `--secret-pos 0`. The whole int32 coefficient is overridden when the buffer is coefficient-shaped, otherwise one byte. The values are `domain_values(dist, --field-mod 256)`, with 0 first. |
| buffer fill | the calibrated active length of each input buffer (QEMU-test's binary search) |
| compared output | the first output buffer, as int32 words with mod-q differences if coefficient-shaped, otherwise bytes with xor (`derive_all`) |
| stopping | secret values in order; stop when **both** ineffective and correction have a witness. A crashed trial skips that secret value. A trial that runs past `--trial-timeout 300` s aborts the instruction (`timeout`). |
| direct_leak | up to 16 trace pairs from that sweep, round-robin over the (p, r) directories; secret-buf first, then eph_secret |

These ports were checked against QEMU-test's own code:
- QEMU-test's `run_trial` writes byte-identical inputs on 138 (function, seed, draw, secret) and probe cases;
- `domain_values`, `infer_field_mod` and coefficient-shape detection agree on all 43 distribution strings.

There is one deliberate difference. QEMU-test enters the function through a generated `main()`; here it is called directly with the same argument values and buffer contents. The unfaulted runs depend only on (s, p, r), so they are computed once per function and shared by its instructions. All functions' instructions go into one work queue, costliest first.

**Argument layout.** QEMU-test reads it from per-function `qemu_witness.json` files. Those are not available here, so `qemu_inputs.IRLayout` rebuilds the layout the way the harness generator (`Dilithium-LLVM/llvm/fault-types`, `createDynamicDriverFunction`) and `extract_qemu_witness.py` would:
- argument order and roles from the function_inputs sample;
- buffer lengths from the alloca that the first call site's argument traces back to.

Pass `--witness-dir` (QEMU-test's `tests_dilithium/`) to use the real files instead. See the caveats.

## Usage
Run from this folder. Everything the tool reads is in here, and all paths are relative to it.
```bash
pip install -r requirements.txt

# 1. find: skip every instruction of every function (inputs and config default to data/ and config/)
python find_faults.py --elf elf/dil2_O0.elf --ir elf/dil2.relabel.ll --out out/O0 --jobs 10 \
    --functions "$(cat config/functions.txt)"          # or e.g. --functions poly_add,ntt

# 2. correlate, compare builds, compare timing
python correlate.py map --faults out/O0/machine_faults.csv --elf elf/dil2_O0.elf \
    --ir elf/dil2.relabel.ll --report data/fault_report_dilithium.csv --out out/O0
python correlate.py compare out/O0/report_rows.csv out/O3/report_rows.csv --out out
python correlate.py timing --ir-timing data/timing_dilithium.csv \
    --ours out/O0/timing_dilithium.csv out/O3/timing_dilithium.csv \
    --report data/fault_report_dilithium.csv --out out
```
Without `--functions`, all 39 config functions run, including `signature_internal` (many hours). `config/functions.txt` leaves it out. `find_faults.py` checkpoints every finished instruction, so rerunning the same command resumes.

**Rebuilding the ELFs (optional).** The ones used are in `elf/`. `build_elf.py` lowers the IR the study faulted and needs `arm-none-eabi-gcc`. `build_os_clang.sh` compiles the C sources with clang `-Os`, and needs a Dilithium-LLVM checkout and clang as well.
```bash
python build_elf.py data/dilithium2.ll -O 0 --relabel -o build/dil2_O0.elf    # also -O 3, -O s
./build_os_clang.sh <Dilithium-LLVM dir> build/dil2_Os_clang.elf
```
`--relabel` also writes the relabelled IR, which is the same file for every `-O` (`elf/dil2.relabel.ll`). `dil2_Os_clang.elf` is not built from that IR; pass it as `--ir` only for the argument layouts, and don't run `correlate.py map` on that build.

To get hardware counters, run step 1 under `perf stat -e task-clock,cycles,instructions -o perf_O0.txt python find_faults.py ...`, using `--jobs 1` for a like-for-like comparison with QEMU-test's single process. This needs `linux-tools-$(uname -r)` and `kernel.perf_event_paranoid <= 1`.

## What is in here
| | |
|---|---|
| `reviews/` | two independent reviews by AI agents (bias, code), made on the version before the fixes |
| `results/{O0,O3,Os,Os_clang}/`, `results/*.csv` | the CSVs behind the report |
| `config/` | `dilithium.json` (the IR-level run's config), `functions.txt` (the 38 functions passed to the sweep) |
| `data/` | `function_inputs/`, `dilithium2.ll`, `fault_report_dilithium.csv`, `timing_dilithium.csv` (IR-level timing) |
| `elf/` | the four ELFs and the relabelled IR |

## Outputs
| file | one row per | notes |
|---|---|---|
| `machine_faults.csv` | (function, pc) | `fired`, `trials`, `sv_examined`, `crash`, `timeout`, the three verdicts + details, `seconds`, `cpu_seconds`, `emu_insns` |
| `timing_dilithium.csv` | function | QEMU-test's `function,status,seconds`. `seconds` is sequential-equivalent: calibration + unfaulted runs + every instruction's sweep. It also has `cpu_seconds`, `n_pcs`, `trials` and `emu_insns`. |
| `layouts.json` | function | the argument layout used (and where it came from), active lengths, sweep setup |
| `correlation.csv` | (PC, report row) | `kind` = `matched`, `machine_only` (no IR fault at that IR instruction, or no IR instruction) or `ir_only` (no PC maps to it) |
| `report_rows.csv` | report row | `n_pcs` mapped, `n_pcs_detected`, `mc_any` (yes / no / unmapped / not swept), `agree` |
| `opt_compare.csv` | report row | each build's `n_pcs`, `n_detected` and `mc_any` side by side |
| `timing_compare.csv` | function | IR-level seconds (and per IR fault) against ours (and per PC) |

## Files
`emu.py` is the Unicorn Cortex-M4 (MPS2-AN386) machine and the `Skip` fault, trimmed from ucpqc. The other files:

| file | contents |
|---|---|
| `qemu_inputs.py` | QEMU-test's samplers, layout, calibration and trial inputs |
| `tests.py` | QEMU-test's three tests |
| `ir.py` | IR indexing, `relabel`, PC → IR through DWARF (including inlined code) |
| `find_faults.py`, `correlate.py` | the two steps |
| `build_elf.py`, `build_os_clang.sh`, `mps2.ld`, `startup.c` | optional ELF builders |

## Caveats
- **The rebuilt argument layout is unverified.** Without the witness files, the layout follows the generator's rules. Two of those rules can't be checked here:
  - which call site the generator picks (LLVM's use-list order);
  - the alloca size behind it. For example, `poly_add`'s buffers are 4096 bytes because the chosen call site passes rows of a `polyvecl`.

  A different length changes how much of the RNG stream each buffer uses, so the concrete values would differ while the distributions, counts and tests stay the same. Each layout and its traced root are in `layouts.json`.
- **Six config functions have no `function_inputs` sample** in `data/function_inputs`: `keccak_absorb`, `decompose`, `power2round`, `polyveck_chknorm`, `polyvecl_chknorm` and `unpack_sk`. They are reported as errors.
- **Fault models differ.** The IR models (`zero`, `undef`, `opA`, `opB`, `mem`) replace a value; a skip leaves the old register or memory contents instead. Registers a call has not set yet are zero at entry here, so a skipped first write often reads as 0; on hardware they hold whatever the caller left. Both act once per call: the IR generator unrolls constant-trip loops and faults the first iteration only, and a skip acts on the first execution.
- **"-O0" means no further optimisation of `dilithium2.ll`.** The generator (`llvm-gen.cpp`) already ran mem2reg, SROA, SCCP, InstCombine, SimplifyCFG, IPSCCP and IndVarSimplify on it.
- **Optimisation changes the mapping.** At -O3 one IR instruction can become many PCs (unrolling) or none. Inlined callee code is attributed to the callee.
