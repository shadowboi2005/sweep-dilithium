# Code review: correctness findings

Snapshot: `review/snapshot`. All emulation ran on the server in `~/review_code` (unicorn 2.1.4, capstone 5.0.7, Python 3.12). My scripts and raw outputs are next to this file (`code/`).
Status: **CONFIRMED** = reproduced or proven from code/data. **PLAUSIBLE** = mechanism shown in the code, wrong result not observed.

## Summary

| # | Sev | Where | Finding | Status |
|---|---|---|---|---|
| 1 | High | emu.py:343-348 | Skipping an instruction inside an IT block does nothing, but is recorded as `fired` | CONFIRMED |
| 2 | High | correlate.py:83, 116-118 | `mc_any`/`agree` mix in verdicts from a different function's test (inlined copies). O3 14 of 36 and Os 13 of 30 "yes" rows exist only because of this | CONFIRMED |
| 3 | High (process) | find_faults.py:259-261 | Checkpoint key omits tool version, IR, config content, inputs and timeout, so stale rows are reused (also after the register fix) | CONFIRMED |
| 4 | High (interpretation) | README "Fault models differ" | README says an IR fault acts on every execution. The generator unrolls and faults iteration 0 only | CONFIRMED |
| 5 | Medium | emu.py:286-305 | Pending IT state survives a crash and makes the next call's first instructions conditional. Zeroing r4-r12 and APSR does not clear it | CONFIRMED |
| 6 | Medium | qemu_inputs.py:625-629, 646 | Memory is not restored between trials: above/below the stack window, scratch, flash past .data, .bss. Faulted outputs depend on earlier trials | CONFIRMED (outputs, witness details); verdict flip not seen |
| 7 | Medium | find_faults.py:276-327 | Resume reports `golden_seconds` = 0. The shipped O0 timing has this for 13 of 32 functions | CONFIRMED |
| 8 | Medium | qemu_inputs.py:214-217 + config | `open` sweep is vacuous: secret `sm` has active length 0, so no secret value is ever written. The config is not the IR-level run's config | CONFIRMED |
| 9 | Medium | find_faults.py:170-184 | direct_leak fallback differs from QEMU-test (16 vs 4 pairs, "skip" vs "error"), and its reference run has the fault hook armed | CONFIRMED / PLAUSIBLE |
| 10 | Low | correlate.py:117-118 | `mc_any` turns timeout, skip, crash-only and never-executed into "no", which counts as agreement | CONFIRMED |
| 11 | Low | emu.py:204-210 | `emu_insns` over-counts by the rest of the block for each skip | CONFIRMED |
| 12 | Low | emu.py:310-311 | Any early stop that is not an error is reported as a timeout and aborts the PC | PLAUSIBLE |
| 13 | Low | ir.py:261-281 | Prologue PCs carry the C scopeLine, which relabel leaves alone; it can alias an IR index | Latent (no swept function hit) |
| 14 | Low | several | CSV overwritten by a subset run, duplicate report row, `cpu_seconds`, build scripts, README | CONFIRMED |

---

## 1. Skip inside an IT block is a silent no-op (High, CONFIRMED)

**Where:** emu.py:343-348 (`Skip._on_pc`). Unicorn calls a code hook for a predicated instruction, but neither writing PC nor `emu_stop()` from that hook takes effect. The instruction executes normally and `fired` is incremented.

**Reproduction** (`it_test.py`): `cmp r0,#0; ite eq; moveq r1,#1; movne r1,#2; mov r0,r1; bx lr`, entered with r1=9.

| skipped | r0 | expected | tool |
|---|---|---|---|
| moveq | 0 | 9 | **1** (fired=1) |
| movne | 1 | 9 | **2** (fired=1) |
| second of `itt eq; moveq r1,1; moveq r2,5` | 0 | 1001 | **6** (fired=1) |

(For the first of that `itt` the expected value is 105 and the tool also returns 6.)

**On the shipped ELFs** (`exp1.py` mode 1). I compared the hook against an independent implementation (non-IT: stop at the PC, replace with NOP for one execution, resume; IT: NOP-patch until the predicated instruction has executed once), with full state reset per trial, one input per PC.

| | IT-member PCs | fired | hook output = unfaulted | true skip changes output or crashes | 
|---|---|---|---|---|
| Os | 22 | 19 | 19 | 19 |
| Os_clang | 5 | 3 | 3 | 2 |
| O3 | 181 | 112 (on that input; 142 in the CSV) | 105 | 111 |
| O0 | 0 | - | - | - |

Of the 7 fired O3 members where the hook did something, 5 changed the output (whether identically to a true skip was not compared) and 2 crashed.

Those two O3 crashes (`poly_make_hint` 0x161fc, `open` 0xf356) are wrong: a true skip returns normally. The CSV has them as 256 crashes each.

**Effect on verdicts** (`exp3.py`: the tool's own `_pc`, with only `Skip` replaced by the correct one; PCs with fired>0):

| | PCs | wrong row in machine_faults.csv | no -> yes | other |
|---|---|---|---|---|
| Os | 19 | **18** | 9 PCs (ine 6, cor 3, dl 4) | 9 become crash (`popne`/`bxeq` loop exits) |
| Os_clang | 3 | **2** | 2 PCs (ine) | |
| O3 | 142 | **42** | 41 PCs (ine 6, cor 3, dl 34) | 2 spurious crashes removed |

Examples at Os: `poly_caddq` 0x1830 `add r3,r2` (the conditional +q, the textbook ineffective fault), `poly_decompose` 0x1940, `poly_uniform` 0x1a4e, `poly_make_hint` 0x1990. Shipped verdicts for all fired IT members: not one "yes".
None of these PCs has a report row at its IR instruction (all are `machine_only`), so report_rows.csv does not change. machine_faults.csv, every machine-level count derived from it, and the machine-only discussion do.

**Scope check.** For all non-IT PCs in all four builds (about 12,000) the hook and the independent implementation agree, except for a handful of wall-clock timeout/crash borderline cases. The bug is confined to IT blocks. Skipping the `IT` instruction itself is handled correctly.

**Fix:** do not skip IT members by writing PC. Replace the bytes with NOP (`bf00`, or `f3af 8000` for 32-bit), detect that the predicated hook fired, then stop at the first instruction after the block, restore the bytes and resume. `exp1.py: AltSkip` does this. Add a regression test with an `ite` block.

## 2. `mc_any`/`agree` count PCs tested in another function (High, CONFIRMED)

**Where:** correlate.py:83 (`pcs_at[(ir_fn, idx)]`), :85 and :116-118. Inlined code is attributed to the callee, so the report row of `poly_add` collects PCs inside `polyveck_add`/`polyvecl_add`. Those PCs' verdicts come from a different test: a different secret buffer, output and word size (polyvecl_add compares bytes), and J=4 ephemeral draws. They say nothing about poly_add's IR verdict on poly_add's test.

**Evidence** (`corr.py`, and recomputing from correlation.csv with own-function PCs only):

| | yes->yes | yes->no | no->yes | no->no |
|---|---|---|---|---|
| O3 shipped | 14 | 3 | 22 | 68 |
| O3 own PCs only | **8** | **9** | **14** | **76** |
| Os shipped | 17 | 5 | 13 | 80 |
| Os own PCs only | **12** | **10** | **5** | **88** |

14 O3 and 13 Os report rows are "yes" only through foreign PCs (e.g. `poly_add` line 8 ineffective and correction: 16 own PCs, 176 foreign at O3). At O0 there are none. The reproduction rate of mapped IR "yes" rows drops from 14/17 to 8/17 at O3 and from 17/22 to 12/22 at Os.

**Fix:** restrict `ms` at line 116 to `f["function"] == r["function"]` for `mc_any`/`agree`, and report foreign-function PCs in separate columns (or say clearly that they are included). Do the same for `kind=matched` rows.

## 3. Stale checkpoints are accepted (High for the planned re-run, CONFIRMED)

**Where:** find_faults.py:259-261. The key holds the ELF path and mtime, seeds, eph draws, field-mod, pos, traces, witness path and config *path*. It omits:
- the tool's own code (emu.py, tests.py, ...) and library versions;
- the content of the config;
- `--ir` (layouts), `--inputs` (samples);
- `--trial-timeout`;
- the content of the witness directory.

**Reproduction:** run `poly_ntt` with a config, then edit that config (add `"eph_secret":"a"`) and rerun with `--trial-timeout 1` in the same `--out`. Output: `3 instructions (3 done), ... x 16 (p, r)`. The old rows were accepted (trials=20 for a grid that now needs 16 per value).

**Consequence:** after the register-leak and IT fixes, resuming into an existing out directory silently keeps every old verdict. Conversely, copying the directory changes the mtime and rejects valid rows.

**Fix:** key on content hashes (ELF, IR, config entry, sample, tool sources, unicorn/capstone version) plus trial-timeout. At minimum, use fresh `--out` directories for the re-run.

Related: a truncated last line (killed mid-write) makes the first row appended after it unreadable too, since the file is reopened without a newline. That row is just recomputed on the next resume.

## 4. README's fault-model caveat is wrong (High for interpretation, CONFIRMED)

README: "An IR fault changes the instruction itself, so it acts every time that instruction runs; a skip acts on its first execution only."
The generator fully unrolls constant-trip loops (`binOp_load_store_Fault.cpp`, `LabeledUnrollPass`, trip <= 2000) and then faults one clone: `findInstByLocator` prefers the block named `iter_0_*`/`*.iter0` (`isFirstIterationBlock`). `loop_fn_Fault.cpp` erases the call only in iteration `i == 0` (line 211). So for these loops the IR fault acts on the **first iteration only**, which is close to the tool's hit=1.
Data: `poly_add_line8_opB` direct_leak changes output bytes [0, 1, 2] only, i.e. one of 256 coefficients.
Exception: loops without a constant trip count or over the limits keep one instruction that acts on each run.
The locator also matches by source line:column and kind, not by index. I checked all 100 report sites: no earlier same-kind instruction shares the line:column, so the index join is not harmed.

## 5. IT state leaks across calls (Medium, CONFIRMED)

**Where:** emu.py:286-305. A crash inside an IT block leaves condexec bits set (CPSR 0x66000df3 after the crash). The next `call()` executes its first instructions as if still inside that block.
**Reproduction** (`it_test2.py`, `it_fix.py`): a target of five `adds r0,#1` returns **2 instead of 5** after a crash inside `itttt eq`.

Which resets clear it (tested):

| reset | next call (5 = correct) |
|---|---|
| none | 2 |
| **r4-r12 = 0 and APSR_NZCV = 0** | **4** |
| APSR = 0 | 4 |
| CPSR = 0x1f3 | 5 |
| XPSR = 0x01000000 | 5 |
| `context_save`/`context_restore` | 5 |

So the fix in progress (registers and flags) is **not sufficient** unless it writes XPSR or the context. Only `context_restore` also restored S16 (VFP registers; also FPSCR, PRIMASK, CONTROL). No VFP instructions occur in O3's `poly*` functions, so the VFP leak has no effect there. The bug needs a crash inside an IT block, which is rare in the shipped runs because (finding 1) those skips did nothing; after finding 1 is fixed it becomes more likely.
**Fix:** `ctx = uc.context_save()` once after construction, `context_restore(ctx)` at the start of every call.

## 6. Memory is not restored between trials (Medium, CONFIRMED)

**Where:** qemu_inputs.py:566, 625-629 and 646. Restored: the file bytes of the PT_LOAD segments and the 256 KiB below SP. Not restored:
- SRAM at and above SP (0x203C0000 up), i.e. the caller frame a mis-skipped prologue/epilogue pops from;
- SRAM more than 256 KiB below SP;
- scratch beyond the buffers just allocated, and the padding between them;
- flash beyond the segments' file bytes, including .bss (`_image` holds `s.data` only), plus the trampoline page;
- `Peripherals.regs`, `_reload` and `uart_out`.

The `Harness` docstring ("trials are independent of their order") is therefore false even with registers fixed.

**Evidence** (`exp1.py` mode 2). Registers, XPSR and VFP are reset in both arms, so only memory differs. Each PC is run in order on one machine, against the same trials after a full memory restore. Trials whose output or crash status differed:

| build | differing trials | PCs | example |
|---|---|---|---|
| O0 | 49 | 7 | ntt 0x211c, 0x2166, 0x216e; poly_uniform_gamma1 0x17ca |
| Os | 68 | 7 | ntt 0x2214, 0x221a, ...; poly_uniform_gamma1 0x1bc4 |
| Os_clang | 48 | 6 | ntt; polyvecl_pointwise_acc_montgomery 0x2268 |
| O3 | 19 | 6 | polyveck_ntt 0x12ba4; polyvecl_pointwise_acc_montgomery 0xf8e0 |

A few crash/timeout differences among these are the 4 s cap I used. Above-SP memory was dirty after the sweep in 31 (O0) functions.
**Effect:** I re-ran full sweeps on the affected PCs of eight function/build pairs (ntt in O0/Os/Os_clang; polyveck_ntt, polyvecl_ntt and pointwise_acc in O3; pointwise_acc in Os_clang; poly_uniform_gamma1 in Os_clang/O0). **No yes/no verdict changed.** One witness did (O0 poly_uniform_gamma1 0x17ca: s1/s2 swapped). So this is a reproducibility defect, not a demonstrated verdict error.
The unfaulted runs were unaffected: after all faulty trials the unfaulted call still matched golden for every function. Golden results come from a separate pool, so a faulty trial cannot corrupt the cache.
**Fix:** snapshot and restore all writable regions (about 9 MB per trial is acceptable on this machine; or track dirty pages), including .bss and the trampoline.

## 7. Resumed runs under-report timing (Medium, CONFIRMED)

**Where:** find_faults.py:276, 298-305, 323-327. Golden runs are repeated only for functions with unfinished PCs, and their time is not checkpointed, so a resumed run writes `golden_seconds = 0` and a `seconds` without it. (Reproduced: golden 0.143 -> 0.0 on rerun.) Partially resumed functions do get a fresh golden time, but calibration is always re-measured.
**Effect on shipped data:** `results/O0/timing_dilithium.csv` has golden_seconds = 0 for 13 of 32 functions (poly_add, polyvecl_add, poly_ntt, invntt_tomont, ntt, open, poly_caddq, poly_decompose, poly_invntt_tomont, poly_make_hint, poly_pointwise_montgomery, poly_sub, poly_uniform), so the O0 run was a resume. Other builds have none. This contradicts the README ("seconds = calibration + unfaulted runs + every sweep"). Size is about 1-5% per affected function (estimate).
Also `cpu_seconds` adds **wall** time for calibration and golden runs, and exceeds wall time on 99% of rows (1032 of 1043 at O0), probably because process_time includes Unicorn's timeout thread. It is not a clean CPU cost. `timing` also lists `elapsed_seconds` columns that find_faults never writes (empty).
**Fix:** store t_gold/t_cal in the checkpoint, or mark timing invalid when resumed.

## 8. `open` is swept without varying the secret (Medium, CONFIRMED)

`results/*/layouts.json`: active lengths for `open` are `sm: 0, pk: 0`. `apply_override` (qemu_inputs.py:216) only writes when `pos < len(vals)`, so for `sm` the override never happens and all 256 "secret values" give the same input. The ineffective test cannot succeed. Every `open` row except one correction hit per build is "no" (or "skip"), and it costs 1441 s at O0. (Probably because a random `sm` is always rejected with the same output, so calibration sees no sensitivity.)
The IR-level study disabled this function: both QEMU-test copies spell the key `pqcrystals_dilithium2_ref_open(calibrate error)`. The report has no `open` rows. So README's "`config/dilithium.json` (the IR-level run's config)" does not hold: the snapshot renames that key and adds six functions (keccak_absorb, decompose, power2round, polyveck_chknorm, polyvecl_chknorm, unpack_sk). The added `output` entries are never read.
**Fix:** exclude `open`, or fail when the secret buffer's active length does not cover `secret_pos`.

## 9. direct_leak fallback differs from QEMU-test (Medium)

Where: find_faults.py:170-184.
- **CONFIRMED:** QEMU-test collects `FALLBACK_PAIRS = 4` pairs; the port collects `--traces` (16). Rows where the fallback produced 16 pairs: O0 5, O3 35, Os 2. "Any trace changed" and "all changed traces leak" depend on the number of traces.
- **CONFIRMED:** a crash during collection gives QEMU-test verdict `error`; the port writes `skip` (195, 379, 99 and 137 rows at O0/O3/Os/Os_clang). No correlated report row is affected.
- **PLAUSIBLE:** the reference run `g = h.run(...)` runs with the fault hook attached and without `skip.reset()`. It relies on `hits >= 1` left by the previous trial. If the previous faulty run did not reach the PC (`hits == 0`), the reference is itself faulted, so "no change" is reported. It also runs on the machine dirtied by the sweep's crashes (findings 5, 6).
**Fix:** detach the skip (or use a separate clean machine) for `g`; use 4 pairs and `error`.

## 10. `mc_any` coercion (Low, CONFIRMED)

correlate.py:117-118: anything not "yes" becomes "no", including timeout, skip, crash-only PCs and PCs never executed. Then `agree` is "yes" against an IR "no".
Shipped impact is small: no report row is all-timeout or all-crash. Two O0 rows (`poly_uniform` line 24 fnSkip, both tests; PCs 0x15bc-0x15c6) are "no / agree=yes" although none of the five PCs ever executed. Crash-only PCs are still counted in n_pcs (matched rows with a "no" and sv_examined = 0: O0 49, O3 48, Os 2).
**Fix:** `mc_any` in {yes, no, timeout, crashed, not executed, unmapped}.

## 11. `emu_insns` over-counts (Low, CONFIRMED)

The skip ends the translation block, and the remainder is re-entered as a new block, which `_hook_block` counts again (emu.py:204-210). poly_add O0 per-trial counts for consecutive straight-line PCs decrease 4115, 4114, 4113, ... (they should be constant); later ones cross blocks. Error is up to one block per trial, worst at O3 with long unrolled blocks. Only affects the `emu_insns` columns.

## 12. Non-error early stop is reported as timeout (Low, PLAUSIBLE)

emu.py:310: if emulation stops for any reason without an exception, with PC not at the trampoline, the tool raises EmulationTimeout and the whole PC becomes `timeout`. QEMU-test (`run_trial`) requires `elapsed >= 0.99*timeout`, else it reads outputs. Also the QEMU-test run does not fail on semihosting exit (it reads outputs); the tool crashes. No shipped row is affected: the only timeouts (2, O3) ran the full 300 s.

## 13. Prologue rows alias IR indices (Low, latent)

`relabel` sets instruction lines but keeps DISubprogram scopeLine. Prologue PCs get line = scopeLine. If scopeLine-1 < number of IR instructions, the prologue is mapped `exact` to an unrelated IR instruction. I checked all functions: in the shipped data only non-swept functions qualify (KeccakF1600, pack_sk, ...), and none of them appears in a correlation. Guard: reject or rewrite the scopeLine.

## 14. Smaller items (Low, CONFIRMED)

- Running a subset in an existing `--out` overwrites `machine_faults.csv`/timing with only that subset (layouts.json is merged; verified with poly_ntt then poly_invntt_tomont).
- `cmd_compare` keys on (fault, test): the report contains a duplicated row (signature_internal polyveck_invntt_tomont line61 direct_leak), so opt_compare.csv has 316 rows, not 317.
- `build_elf.py:109-112`: a .text mismatch only prints a warning and exits 0. `link()` hides linker stderr.
- `build_os_clang.sh`: links `$OBJ/*.o`, so stale objects from an earlier build in the same directory are included.
- `_pc` has no try/finally around `skip.detach()`, and any unexpected Python exception in a worker aborts the whole sweep. Nothing is lost (checkpoint), but the run stops.
- README lists REPORT.md, which is not in the snapshot. "39 config functions, 38 in functions.txt, six without samples" is correct.

---

## Checked and found correct

- **Samplers, `domain_values`, `infer_field_mod`, coefficient shape** (`port.py`): identical to the pinned `distributions_unicorn.py` for every table entry plus unknown/qualified strings, 10 lengths, 8 counts (450 sampler cases).
- **IneffectiveTracker** (3000 random cases), **direct_leak verdict and detail strings** (4000) and **trace-pair selection** (200 directory trees): identical to QEMU-test. `decode_words` (signed int32), `compute_delta` (mod q), `get_buffer` and `derive_all` (first output, length, word size, mode) read line by line against the source: same. EarlyStopSweep logic, stop rule, crash-skips-value and timeout-aborts-PC: same.
- **Trial inputs:** scalars then pointers in layout order, main-RNG draw discarded for eph buffers, eph seed `"eph:j"`, override in scalar_ptr, probe mode with seed/co-seed: same as `run_trial`. Override never changes RNG consumption.
- **Calibration:** same binary search, thresholds and retries (code read).
- **Layout rules** against `createDynamicDriverFunction`, `traceArgToRoot`, `pickCallSite`, `inferPointerAllocSize` and `extract_qemu_witness.py`: consistent (use-list argument holds; no constant-expression GEP call operands in the module, the one place the port differs). Zero-filled buffers match the harness memset. layouts.json is identical across the four builds. Inputs: dilithium2.ll and the fault report are byte-identical to the originals.
- **AAPCS call** (including stacked args and 8-byte alignment), ELF .data/.bss loading, function ranges: the CSV PCs of every build equal objdump's instruction starts exactly (none missing, none inside literal pools, no size mismatch; literal pools are outside symbol sizes).
- **Skip for non-IT instructions:** agrees with an independent implementation on one input for every PC of every build, including first-instruction, 16/32-bit, branches and the `IT` instruction itself.
- **PC -> IR mapping:** equals `addr2line -i` for every PC of O0, Os and O3 (one addr2line line-0 case, where the decoded line table agrees with the tool). `relabel` of dilithium2.ll reproduces `elf/dil2.relabel.ll` exactly; instruction lists are unchanged; all 100 report sites have the right instruction kind and callee; no multi-line instructions, no inlinedAt.
- **parse_fault** on all 317 report rows; `_agree` for timeout rows.
- **machine_faults.csv <-> timing_dilithium.csv:** per-function sums, n_pcs, trials consistent (apart from finding 7). Function sizes in dil2_Os.elf total 2812 B as documented.
- **Golden cache keying and pool globals:** (idx, p, j) per function, computed before fork of the second pool, not modified by faulty trials.

## Not checked

- Rebuilding the ELFs: llvmlite is not on the server, and I did not compile locally. The shipped ELFs therefore were not shown to be reproducible from build_elf.py/build_os_clang.sh (only producer strings, sizes and the relabelled IR were checked).
- Real QEMU-test trials against this tool end-to-end (no witness files exist); the README's "138 cases" claim.
- The register-leak effect itself (known issue; skipped as instructed).
- All findings 1 and 6 sweeps used my own reset of registers/XPSR/VFP, a 4-10 s trial cap, and (finding 6, output-level) at most 12 trials per PC.
