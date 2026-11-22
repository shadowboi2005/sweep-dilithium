# Do LLVM-IR fault findings show up as instruction skips?

Dilithium2, Cortex-M4 code under Unicorn. Every machine instruction of 31 functions is skipped once (first execution) and scored with QEMU-test's three tests. The search uses no IR-fault information. The verdicts are then joined with `data/fault_report_dilithium.csv` through DWARF. Figures come from the CSVs in `results/` unless stated. "Controls" are experiments by the second review (`reviews/REVIEW_BIAS_2.md`), on stated subsets and the same sweep code apart from the fixes listed under History.

## Summary
1. **At -O0, skipped calls carry over.** 29 of 30 IR-positive call-skip rows reproduce; random instructions do clearly worse (66 of 70 rows agree, against 50 to 54). Skipping a `bl` is the same fault as removing the call, so this mostly checks the pipeline. The 30th is a set-up mismatch (`poly_ntt`, below).
2. **At -Os and -O3 the call-skip agreement (17/20, 11/12) is not evidence:** random instructions of the same functions do about as well.
3. **Skipping an instruction does not reproduce what the IR value faults found.** The sweep only skips instructions and never injects a value fault (see "What is injected"), so this does not show that value faults fail at machine level. Agreement is below always answering "no" in all three builds (69, 84 and 73 of 95, against 85), and the mapped instructions do no better than one random instruction of the function (81.7, 81.1 and 81.9 on average; only Os is above, within chance). Only 4, 2 and 2 of the 10 IR-positive rows reproduce. Those 10 rows are 4 sites in 3 functions, so the rates themselves are very uncertain.
4. **Overall agreement (82%, 88%, 79%) is weak evidence.** Always "no" scores 76%, 81% and 84%. Of the 40 IR-positive rows in swept functions, 33, 13 and 8 reproduce.
5. **Which operand a skip leaves behind depends on the compiled code.** Skipping the `add` of `poly_add` leaves `a` at -O0 and the secret `b` at -Os.
6. **The counts of detected instructions are not counts of vulnerabilities.** Many detections occur without any secret, or depend on zeroed registers (Limits).

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
- **What follows.** Agreement on call-skip rows compares like with like. Agreement on value-fault rows asks whether a skip *stands in for* a value fault. Whether the value faults themselves survive compilation would need injecting them: for `opA`, `opB` and `zero`, overwriting the destination register with the value the model gives; for a load, writing 0. `undef` has no machine value. This was not done.

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

## Agreement with the IR report
An IR "yes" row is reproduced if any instruction of the row's own function that maps to its IR instruction is detected. An IR "no" row agrees if none is. Rows of `signature_internal` (150), rows with no mapped instruction, and rows whose instructions never ran are left out. `results/stats.csv` has every set.

| | O0 | Os | O3 |
|---|---|---|---|
| rows compared | 165 | 115 | 107 |
| agree | 135 (82%) | 101 (88%) | 84 (79%) |
| always "no" | 125 (76%) | 93 (81%) | 90 (84%) |
| as many random instructions of the function (mean agreement) | 135.7 | 97.9 | 51.9 |
| one random instruction per row (mean agreement) | 131.8 | 94.9 | 92.7 |
| precision / recall | 0.59 / 0.83 | 0.72 / 0.59 | 0.36 / 0.47 |
| kappa | 0.56 | 0.58 | 0.28 |
| IR "yes" rows reproduced, of the 40 in swept functions | 33 | 13 | 8 |
| of those 40, no mapped instruction | 0 | 18 | 23 |
| (IR site, test) units that agree | 86 / 96 | 37 / 46 | 29 / 38 |

At O3 the "as many random instructions" baseline is so low because a row maps to up to about 200 instructions and any "yes" counts. With one random instruction per row, random choice agrees more often than the mapped instructions.

**By fault type**

| | O0 | Os | O3 |
|---|---|---|---|
| call skips: agree | 66 / 70 | 17 / 20 | 11 / 12 |
| … always "no" | 40 | 8 | 5 |
| … random, as many / one (mean) | 53.9 / 50.2 | 17.1 / 13.9 | 11.0 / 10.8 |
| … IR "yes" reproduced | 29 / 30 | 11 / 12 | 6 / 7 |
| value faults: agree | 69 / 95 | 84 / 95 | 73 / 95 |
| … always "no" | 85 | 85 | 85 |
| … random, as many / one (mean) | 81.8 / 81.7 | 80.7 / 81.1 | 40.8 / 81.9 |
| … IR "yes" reproduced | 4 / 10 | 2 / 10 | 2 / 10 |
| without `*uniform*`: agree (always "no") | 119 / 147 (121) | 89 / 101 (90) | 78 / 101 (90) |
| direct_leak (7 rows): IR "yes" reproduced | 0 / 2 | 2 / 2 | 1 / 2 |

- **Reproduced means the same thing.** For 30 of the 33 O0 rows the witness (position, secret values, alpha) is identical to the IR report's, and all 29 reproduced call skips hit at the `bl` itself (second review).
- **At Os and O3 every reproduced call skip is in a `*uniform*` function.** At O0, 15 of the 29 are elsewhere.
- **IR "no", machine "yes" at O0 (23 rows).** 8 are one `add` in `invntt_tomont` (line 25), where the skip leaves `a[j+len]`, which is what IR `opB` should give, yet the IR report says "no". This conflict is **unexplained**. 6 are the other models at the one `poly_add` instruction below, 6 the same in `poly_sub`, and the other 3 are in `poly_decompose` and `poly_uniform_eta`.
- **Rows are not independent.** `opA`, `opB`, `undef` and `zero` at one instruction share the same machine instructions.

**`poly_add` line 8.** At O0 the skipped `add r1, r3` leaves `a[i]` in `r1`, which is stored, like the IR model `opA`. At Os, skipping `add r3, ip` leaves `b[i]`, like `opB`, so the secret leaks directly and the `opA` verdicts do not reproduce. No single skip can match all four models.

**`poly_ntt`.** The IR row has a witness (s1=12, position 916) that is impossible with the config's 5-value secret. Treating the secret as bytes reproduces it exactly, so that row was made with other settings. The same change turns `polyveck_ntt` and `polyvecl_ntt` to "yes", where the IR report says "no".

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
- **First output only.** Only the first output buffer is compared. Ineffective compares raw values, not values mod q.
- **No held-out data.** The tool was developed with the IR report in hand, on one scheme and parameter set. An earlier version with other inputs agreed on 93 of 167 rows.
- **Layouts are rebuilt,** because the witness files are not available.
- **Timeouts are wall-clock.** The 4 at O3 are all `pop` instructions in `*uniform*` functions.
- **Not run:** QEMU-test's generated `main()`, `signature_internal`, hardware counters, real hardware.

## History
Two rounds of independent reviews by AI agents are in `reviews/`.
- **Round 1** found that skips inside IT blocks did nothing, that register and memory state leaked between trials, that report rows counted instructions tested under another function's inputs, and that `open` was vacuous. After the fixes everything was re-run: detections gained and lost were 3/3 (O0), 19/0 (Os), 8/1 (Os_clang) and 47/4 (O3). Agreement went from 137/167 to 135/165 (O0), 97/115 to 101/115 (Os) and 82/107 to 84/107 (O3).
- **Round 2** found that a skip could use a wrong instruction size after a trial that overwrote code, and that this report's reading of direct_leak and of the O3 baseline was misleading. After the fix everything was re-run: **no verdict changed in any build.** 3 O0 rows and 1 Os_clang row changed in `emu_insns` only, and so did the 4 O3 timeouts. The wording above is corrected. A later fix to `emu_insns` (a counter only) is not reflected in the CSVs.
