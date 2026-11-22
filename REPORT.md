# Do LLVM-IR fault findings show up as instruction skips?

Dilithium2, Cortex-M4 code under Unicorn. Every machine instruction of 31 functions is skipped once (first execution) and scored with QEMU-test's three tests. The search uses no IR-fault information. The verdicts are then joined with `data/fault_report_dilithium.csv` through DWARF. Figures come from the CSVs in `results/` unless stated. "Controls" are experiments by the second review (`reviews/REVIEW_BIAS_2.md`), on stated subsets and the same sweep code apart from the fixes listed under History.

## Summary
**Question: does an IR fault translate to a real machine fault?** An IR finding translates if skipping a machine instruction compiled from its IR instruction is detected by the same test. There are 40 IR findings (a fault and a test with verdict "yes") in the swept functions.

| | O0 | Os | O3 |
|---|---|---|---|
| **translate** | **33 of 40 (82%)** | **13 of 40 (32%)** | **8 of 40 (20%)** |
| … with the same witness as the IR report | 30 of 33 | 10 of 13 | 7 of 8 |
| do not translate | 7 | 9 | 9 |
| no machine instruction left to skip | 0 | 18 | 23 |

1. **Without optimisation, most IR findings show up as machine faults.** (Emulated: no hardware was used.) 30 of the 33 have exactly the same witness (position, secret values, alpha) as the IR report, so the skip has the same effect, not only the same verdict.
2. **Call skips translate almost completely at -O0: 29 of 30.** The other one (`poly_ntt`) is a set-up mismatch.
3. **Optimisation removes many of them.** At Os and O3, 18 and 23 of the 40 have no machine instruction left, all of them call skips. Of the call skips that are left, 11 of 12 and 6 of 7 translate.
4. **IR value faults translate less: 4, 2 and 2 of 10.** The simulation only skips instructions, so these rows ask whether a skip has the same effect as a different fault (see "What is injected"). They are 4 instructions in 3 functions.
5. **The compiler decides which operand a skip leaves.** Skipping the `add` of `poly_add` leaves `a` at -O0 and the secret `b` at -Os.

## Builds
| build | how |
|---|---|
| `O0` | `dilithium2.ll` (the IR the study faulted), code generation only. The generator had already run a light pipeline on it. |
| `Os` | the same IR, llvmlite approximation of -Os (2,812 B for the 38 config functions; LLVM 20's own `default<Os>` gave 2,816 B, measured once, no data here) |
| `O3` | the same IR, LLVM `default<O3>` |
| `Os_clang` | the C sources, file by file, clang 18 -Os. Swept but not correlated with the IR report. |

## What is injected
**The simulation injects one fault: the skip of one machine instruction.** The function runs normally up to the instruction's first execution, that execution is left out, and everything then continues normally. The fault itself never writes a value. What the program sees afterwards is whatever was already there: the old contents of the destination register, the old memory, or a call that does not happen.

The IR study has several fault types. In this report, a **call skip** is a report row for a removed call (`fnSkip`), and a **value fault** is any other row. These names describe the IR side of the comparison only:

| IR model | what the IR study does (`binOp_load_store_Fault.cpp`, `loop_fn_Fault.cpp`) | rows compared at O0 | what the skip does at the same place | same fault? |
|---|---|---|---|---|
| `fnSkip` | removes a call | 70 | the `bl` is not executed | yes, at the `bl` itself |
| `opA` | sets the second operand of an arithmetic instruction to 0 | 23 | the result register keeps its old value | only by coincidence |
| `opB` | sets the first operand to 0 | 23 | same | only by coincidence |
| `zero` | replaces the result by 0 | 23 | same | only if the old value is 0 |
| `undef` | replaces the result by LLVM's `undef` | 23 | same | not defined |
| `mem` | removes a store, or makes a load return 0 | 3 | the store does not happen, or the register keeps its old value | store: yes; load: only if the old value is 0 |

- **`opA` and `opB` change an operand, not the result.** For `a + b` they give `a` and `b`, for `a - b` they give `a` and `-b`, and for `a * b` both give 0, the same as `zero`.
- **The compiler decides what a skipped computation leaves.** If the destination register held `a`, skipping `a + b` acts like `opA`; if it held `b`, like `opB`; otherwise like neither. The `poly_add` case below shows both.
- **One skip is compared with up to four models.** They share the same machine instructions and can have different verdicts, so some rows must disagree.
- **What follows.** Call-skip rows compare like with like. Value-fault rows ask whether a skip *stands in for* a value fault. Whether the value faults themselves survive compilation would need injecting them: for `opA`, `opB` and `zero`, overwriting the destination register with the value the model gives; for a load, writing 0. `undef` has no machine value. This was not done.

## What the skips find
| build | instructions | never executed | crashed on every secret value | ineffective | correction | direct_leak | any test | of which in `*uniform*` |
|---|---|---|---|---|---|---|---|---|
| O0 | 964 | 53 | 188 | 140 | 116 | 0 | 163 | 79 |
| Os | 704 | 49 | 102 | 165 | 106 | 15 | 189 | 66 |
| Os_clang | 588 | 47 | 133 | 98 | 69 | 3 | 116 | 62 |
| O3 | 9,678 | 144 | 419 | 1,092 | 712 | 236 | 1,482 | 837 |

- **Not comparable between builds.** Unrolling repeats an IR instruction many times, and inlining pulls helper code into the swept functions. The detections fall on 108 (O0), 93 (Os) and 127 (O3) IR sites.
- **Public data.** `poly_uniform` and `polyveck_shiftl` work on public data that the config treats as secret. They supply 17 (O0), 22 (Os) and 287 (O3) detections.
- **direct_leak is undecided** ("error", or a timeout) for 186, 98, 129 and 375 instructions (O0, Os, Os_clang, O3), because the faulted run crashes or hangs.

**direct_leak does not show that optimisation introduces leaks.**
- Only a function's own code is swept. At O0, `poly_add` is tested with `b` as the secret, but `polyveck_add` calls it with the secret as the other operand. Skipping callee instructions under the caller's inputs finds 4 leaking O0 instructions (`poly_add`, `poly_sub`, `decompose`) and 7 in clang -Os (control, 7 callers). Inlining brings such code into the sweep at Os and O3.
- At O0, the skipped `bl` at `0x1460` in `poly_make_hint` leaves the secret in the output, but the sweep stops before the secret has varied, so the verdict is "no". With the secret at another position it is "yes" (control).
- The 15 hits at Os and 236 at O3 are the same 7 IR sites and one unmapped group. 3 and 74 of them leak the ephemeral secret, not the main one.
- Outside `signature_internal`, the IR study has direct_leak rows for `poly_add` only (7).

## Do the IR findings translate?
A finding translates if any instruction of the same function that maps to its IR instruction is detected by the same test. "–" means that no machine instruction maps to it. `results/<build>/report_rows.csv` has every row. The 53 IR findings in `signature_internal` are not included, because it was not swept.

| | O0 | Os | O3 |
|---|---|---|---|
| call skips (30) | 29 yes, 1 no | 11 yes, 1 no, 18 – | 6 yes, 1 no, 23 – |
| value faults (10) | 4 yes, 6 no | 2 yes, 8 no | 2 yes, 8 no |
| ineffective (24) | 20 yes, 4 no | 6 yes, 6 no, 12 – | 3 yes, 6 no, 15 – |
| correction (14) | 13 yes, 1 no | 5 yes, 3 no, 6 – | 4 yes, 2 no, 8 – |
| direct_leak (2) | 0 yes, 2 no | 2 yes | 1 yes, 1 no |
| distinct IR faults (29), in at least one test | 22 | 8 | 5 |
| functions with an IR finding (21), at least one translates | 20 | 7 | 5 |

**Every finding**

| function | IR fault | test | O0 | Os | O3 |
|---|---|---|---|---|---|
| `poly_caddq` | call to `caddq` | ineffective | **yes** | – | – |
| `poly_decompose` | call to `decompose` | correction | **yes** | – | – |
| `poly_make_hint` | call to `make_hint` | correction | **yes** | – | – |
| `poly_make_hint` | call to `make_hint` | ineffective | **yes** | – | – |
| `poly_ntt` | call to `ntt` | ineffective | no | no | no |
| `poly_pointwise_montgomery` | call to `montgomery_reduce` | ineffective | **yes** | – | – |
| `poly_uniform` | call to `dilithium_shake128_stream_init` | ineffective | **yes** | **yes** | – |
| `poly_uniform` | call to `shake128_squeezeblocks` | ineffective | **yes** | – | – |
| `poly_uniform_eta` | call to `rej_eta` | correction | **yes** | **yes** | – |
| `poly_uniform_eta` | call to `rej_eta` | ineffective | **yes** | **yes** | – |
| `poly_uniform_gamma1` | call to `dilithium_shake256_stream_init` | correction | **yes** | **yes** | – |
| `poly_uniform_gamma1` | call to `dilithium_shake256_stream_init` | ineffective | **yes** | **yes** | – |
| `poly_uniform_gamma1` | call to `shake256_squeezeblocks` | correction | **yes** | – | – |
| `poly_uniform_gamma1` | call to `shake256_squeezeblocks` | ineffective | **yes** | – | – |
| `polyveck_caddq` | call to `poly_caddq` | ineffective | **yes** | – | – |
| `polyveck_decompose` | call to `poly_decompose` | correction | **yes** | – | – |
| `polyveck_decompose` | call to `poly_decompose` | ineffective | **yes** | – | – |
| `polyveck_pointwise_poly_montgomery` | call to `poly_pointwise_montgomery` | ineffective | **yes** | – | – |
| `polyveck_reduce` | call to `poly_reduce` | ineffective | **yes** | – | – |
| `polyveck_shiftl` | call to `poly_shiftl` | correction | **yes** | – | – |
| `polyveck_shiftl` | call to `poly_shiftl` | ineffective | **yes** | – | – |
| `polyveck_uniform_eta` | call to `poly_uniform_eta` | correction | **yes** | **yes** | **yes** |
| `polyveck_uniform_eta` | call to `poly_uniform_eta` | ineffective | **yes** | **yes** | **yes** |
| `polyvecl_add` | call to `poly_add` | correction | **yes** | – | – |
| `polyvecl_pointwise_acc_montgomery` | call to `poly_pointwise_montgomery` | ineffective | **yes** | – | – |
| `polyvecl_pointwise_poly_montgomery` | call to `poly_pointwise_montgomery` | ineffective | **yes** | – | – |
| `polyvecl_uniform_eta` | call to `poly_uniform_eta` | correction | **yes** | **yes** | **yes** |
| `polyvecl_uniform_eta` | call to `poly_uniform_eta` | ineffective | **yes** | **yes** | **yes** |
| `polyvecl_uniform_gamma1` | call to `poly_uniform_gamma1` | correction | **yes** | **yes** | **yes** |
| `polyvecl_uniform_gamma1` | call to `poly_uniform_gamma1` | ineffective | **yes** | **yes** | **yes** |
| `poly_add` | `mem`, line 5 | direct_leak | no | **yes** | no |
| `poly_add` | `opA`, line 8 | correction | **yes** | no | no |
| `poly_add` | `opB`, line 8 | direct_leak | no | **yes** | **yes** |
| `poly_add` | `opA`, line 8 | ineffective | **yes** | no | no |
| `poly_pointwise_montgomery` | `undef`, line 10 | correction | no | no | **yes** |
| `poly_pointwise_montgomery` | `opA`, line 10 | ineffective | no | no | no |
| `poly_pointwise_montgomery` | `opB`, line 10 | ineffective | no | no | no |
| `poly_pointwise_montgomery` | `zero`, line 10 | ineffective | no | no | no |
| `poly_sub` | `opA`, line 8 | correction | **yes** | no | no |
| `poly_sub` | `opA`, line 8 | ineffective | **yes** | no | no |

- **What is left at Os and O3 is mostly in the `*uniform*` functions:** 11 of 13 and 6 of 8 (14 of 33 at O0). There the tests also fire on unrelated outputs (Limits), so these are weaker evidence of an exploitable fault.
- **The three O0 translations with another witness** are `poly_decompose` (another alpha) and the two of `poly_uniform_eta` (another position).

**The findings that do not translate at O0**
- `poly_ntt`: the IR row has a witness (s1=12, position 916) that is impossible with the config's 5-value secret. Treating the secret as bytes reproduces it exactly, so that row was made with other settings. The same change turns `polyveck_ntt` and `polyvecl_ntt` to "yes", where the IR report says "no".
- `poly_pointwise_montgomery` line 10 (4 rows): the IR faults zero the product or an operand; the skipped `smull r0, r1, r0, r1` leaves the two operands in the registers of the product.
- `poly_add` line 8, `opB`: the skipped `add` leaves `a`, not the secret `b`.
- `poly_add` line 5, `mem`: the IR fault loads 0, so `b` is stored. The skipped load leaves the address of `a` in the register instead.

**`poly_add` line 8.** At O0 the skipped `add r1, r3` leaves `a[i]` in `r1`, which is stored, like the IR model `opA`. At Os, skipping `add r3, ip` leaves `b[i]`, like `opB`, so the secret leaks directly and the `opA` findings do not translate. No single skip can match all four models.

## Time
| | IR-level | O0 | Os | Os_clang | O3 |
|---|---|---|---|---|---|
| sequential-equivalent seconds, 31 functions | 18,639 | 39,310 | 5,075 | 5,223 | 16,726 |
| faults or instructions | 83 | 964 | 704 | 588 | 9,678 |
| seconds elapsed on 32 cores (`results/elapsed.csv`) | | 2,336 | 421 | 1,028 | 542 |

- **Timing is from the previous run of the sweep** (commit 967f155), when the server was quieter. `timing_*.csv` belong to it. The `seconds` in `machine_faults.csv` are from the final run, on a server shared with other jobs (load up to 93 on 64 threads), and do not add up to the timing files.
- **Only indicative.** The IR-level seconds are from another, unknown machine, with one rebuilt ELF per fault. Ours were measured with 32 processes on a Xeon Silver 4314 (64 threads).
- **Use `seconds`.** `cpu_seconds` in the timing files is 7–13% too high (it included Unicorn's timeout thread), and `emu_insns` over-counts slightly.
- **Most of the O0 time yields no verdict.** 30 of 964 instructions took over 300 s each in that run (33 in the busier final run), 71% of the total; all 30 crash on every secret value, after running away for a long time. The longest took 2,247 s (`pop {r7, pc}` in `polyvec_matrix_pointwise_montgomery`).
- **Example:** `poly_add` takes 46 s for 25 instructions at O0 (1.9 s each), against 197 s per IR fault.

| function | IR s | IR faults | O0 s | O0 instr. | Os s | Os_clang s | O3 s | O3 instr. |
|---|---|---|---|---|---|---|---|---|
| `polyveck_invntt_tomont` | 438 | 1 | 4,945 | 21 | 86 | 265 | 71 | 10 |
| `polyvecl_invntt_tomont` | 434 | 1 | 4,611 | 21 | 90 | 254 | 71 | 10 |
| `polyvecl_uniform_gamma1` | 22 | 1 | 4,478 | 27 | 133 | 159 | 104 | 22 |
| `polyvec_matrix_pointwise_montgomery` | 491 | 1 | 4,407 | 26 | 169 | 398 | 58 | 18 |
| `polyvecl_uniform_eta` | 20 | 1 | 2,706 | 31 | 67 | 91 | 312 | 21 |
| `polyveck_uniform_eta` | 20 | 1 | 2,602 | 31 | 71 | 84 | 319 | 21 |
| `poly_uniform` | 1,059 | 3 | 2,046 | 83 | 1,237 | 750 | 2,703 | 485 |
| `polyvecl_pointwise_acc_montgomery` | 1,062 | 3 | 1,512 | 32 | 382 | 102 | 1,360 | 852 |
| `invntt_tomont` | 3,350 | 18 | 1,504 | 109 | 259 | 324 | 4,145 | 1971 |
| `polyveck_decompose` | 29 | 1 | 1,168 | 27 | 159 | 57 | 755 | 539 |
| `polyvecl_pointwise_poly_montgomery` | 434 | 1 | 1,056 | 26 | 154 | 71 | 448 | 381 |
| `polyveck_add` | 1,202 | 1 | 1,039 | 27 | 380 | 113 | 904 | 307 |
| `poly_uniform_eta` | 437 | 2 | 1,005 | 42 | 426 | 1,006 | 800 | 286 |
| `polyveck_reduce` | 408 | 1 | 987 | 21 | 127 | 51 | 340 | 359 |
| `polyveck_sub` | 1,133 | 1 | 953 | 27 | 289 | 110 | 909 | 307 |
| `polyveck_pointwise_poly_montgomery` | 432 | 1 | 945 | 26 | 150 | 70 | 462 | 381 |
| `polyveck_caddq` | 395 | 1 | 800 | 21 | 131 | 65 | 412 | 359 |
| `poly_uniform_gamma1` | 36 | 2 | 795 | 16 | 357 | 968 | 886 | 427 |
| `polyvecl_add` | 803 | 1 | 588 | 27 | 188 | 68 | 668 | 307 |
| `polyveck_shiftl` | 22 | 1 | 566 | 21 | 106 | 69 | 352 | 439 |
| `polyvecl_ntt` | 29 | 1 | 94 | 21 | 2.1 | 3.9 | 1.5 | 10 |
| `poly_make_hint` | 1,167 | 5 | 75 | 37 | 34 | 27 | 120 | 153 |
| `poly_pointwise_montgomery` | 1,460 | 5 | 70 | 29 | 18 | 29 | 109 | 103 |
| `polyveck_ntt` | 30 | 1 | 68 | 21 | 2.2 | 4.2 | 1.3 | 10 |
| `poly_caddq` | 416 | 1 | 67 | 24 | 12 | 26 | 74 | 89 |
| `poly_decompose` | 428 | 1 | 58 | 28 | 22 | 25 | 158 | 138 |
| `ntt` | 148 | 13 | 49 | 86 | 2.0 | 3.4 | 58 | 1519 |
| `poly_sub` | 907 | 4 | 48 | 25 | 8.1 | 9.7 | 57 | 76 |
| `poly_add` | 1,382 | 7 | 46 | 25 | 11 | 9.9 | 64 | 76 |
| `poly_invntt_tomont` | 420 | 1 | 23 | 3 | 4.6 | 10 | 3.7 | 1 |
| `poly_ntt` | 27 | 1 | 0.6 | 3 | 0.2 | 0.3 | 0.2 | 1 |

The full set of columns is in `results/timing_compare.csv`.

## Limits
- **Coverage.** 31 of 39 config functions, 11.4% of the IR functions' code bytes at O0 (second review). The list comes from the IR study. `signature_internal` was not swept and holds 53 of the 93 IR "yes" rows; an older version of this tool reproduced 2 of those 53 on a reduced sweep. Six functions have no input sample. `open` is excluded because its secret has no effect on its output.
- **The tests fire without a secret** (controls). Fed unrelated outputs, ineffective fires in 10 of 10 runs for the six `*uniform*` functions, both `*decompose` functions and `poly_make_hint`. Correction does so with real unrelated outputs for five of the six `*uniform*` functions, but only 0–2 of 10 for `poly_uniform`. With no secret planted, 55% (O0), 51% (Os), 61% (Os_clang) and 44% (O3) of the correction hits remain. Sweeping a public input instead of the secret triggers ineffective about as often. QEMU-test has the same property.
- **Initial state.** Registers, stack and output buffers start at zero. With random registers and stack, 34 of 156 re-run O0 detections and 21 of 188 at Os disappear, 5 of the 15 Os leaks among them, and one of the two value-fault rows reproduced at Os (controls).
- **One position, one execution.** Only the secret's first coefficient and the first execution are tested. Outside `*uniform*`, with the secret at two other positions, 62 and 64 instructions are found at O0 instead of 82, and 64 and 67 at Os instead of 123. In half of O3 it is 332 and 254 instead of 334 (controls).
- **Unexplained conflict.** Skipping one `add` in `invntt_tomont` (line 25) leaves `a[j+len]`, which is what IR `opB` should give. The skip is detected, but the IR report says "no" for all models there (8 rows).
- **Scope of the question.** The report counts whether each IR finding shows up. It does not measure whether the effect is specific to that instruction; other instructions of the function are often detected too (`results/stats.csv`).
- **First output only.** Only the first output buffer is compared. Ineffective compares raw values, not values mod q.
- **No held-out data.** The tool was developed with the IR report in hand, on one scheme and parameter set.
- **Layouts are rebuilt,** because the witness files are not available.
- **Timeouts are wall-clock.** The 4 at O3 are all `pop` instructions in `*uniform*` functions.
- **Not run:** QEMU-test's generated `main()`, `signature_internal`, hardware counters, real hardware.

## History
Two rounds of independent reviews by AI agents are in `reviews/`.
- **Round 1** found that skips inside IT blocks did nothing, that register and memory state leaked between trials, that report rows counted instructions tested under another function's inputs, and that `open` was vacuous. After the fixes everything was re-run: detections gained and lost were 3/3 (O0), 19/0 (Os), 8/1 (Os_clang) and 47/4 (O3). The IR findings that translate did not change at O0 (33), and went from 17 to 13 at Os and from 14 to 8 at O3, mainly because instructions inlined into other functions no longer count.
- **Round 2** found that a skip could use a wrong instruction size after a trial that overwrote code, and that this report's reading of direct_leak and of a baseline was misleading. After the fix everything was re-run: **no verdict changed in any build.** 3 O0 rows and 1 Os_clang row changed in `emu_insns` only, and so did the 4 O3 timeouts. The wording above is corrected. A later fix to `emu_insns` (a counter only) is not reflected in the CSVs.
