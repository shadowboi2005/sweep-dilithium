# Do LLVM-IR fault findings show up as instruction skips?

Dilithium2, Cortex-M4 code under Unicorn. Every machine instruction of 31 functions is skipped once (first execution) and scored with QEMU-test's three tests. No IR-fault information is used to find faults. The verdicts are then joined with `data/fault_report_dilithium.csv` through DWARF. All figures come from the CSVs in `results/`, run with the code in this repository unless stated.

## Summary
1. **At -O0, call skips carry over.** 29 of the 30 IR-positive call-skip rows reproduce. Skipping a `bl` is the same fault as removing the call, so this mostly checks the pipeline.
2. **Value faults mostly do not.** 4 of 10 IR-positive value-fault rows reproduce at -O0, 2 of 10 at -Os and 2 of 10 at -O3. Agreement on value faults is below always answering "no" in all three builds.
3. **Overall agreement (82%, 88%, 79%) is weak evidence.** Always "no" scores 76%, 81% and 84%. At -O0 and -Os, random instructions of the same function agree about as often as the mapped ones.
4. **The compiler decides which operand a skip leaks.** Skipping the `add` of `poly_add` leaves `a` in the output at -O0 and the secret `b` at -Os.
5. **Optimised builds leak directly.** The direct_leak test fires on 0 instructions at -O0, 15 at -Os, 3 in the clang -Os build and 236 at -O3.

## Builds
| build | how |
|---|---|
| `O0` | `dilithium2.ll` (the IR the study faulted), code generation only. The generator had already run a light pipeline on it. |
| `Os` | the same IR, llvmlite approximation of -Os. LLVM 20's own `default<Os>` gives 2,816 B for the 38 config functions, this one 2,812 B. |
| `O3` | the same IR, LLVM `default<O3>` |
| `Os_clang` | the C sources, file by file, clang 18 -Os. Not comparable to the report's IR, so it is swept but not correlated. |

## What the skips find
| build | instructions | never executed | crashed on every secret value | ineffective | correction | direct_leak | any test | of which in `*uniform*` |
|---|---|---|---|---|---|---|---|---|
| O0 | 964 | 53 | 188 | 140 | 116 | 0 | 163 | 79 |
| Os | 704 | 49 | 102 | 165 | 106 | 15 | 189 | 66 |
| Os_clang | 588 | 47 | 133 | 98 | 69 | 3 | 116 | 62 |
| O3 | 9,678 | 144 | 419 | 1,092 | 712 | 236 | 1,482 | 837 |

- **Counts are not comparable between builds.** Unrolling at -O3 repeats the same IR instruction many times, and inlining pulls helper code into the swept functions.
- **The `*uniform*` functions inflate them.** Their outputs are compared byte by byte, and the ineffective and correction tests fire on most random, unrelated outputs (see Limits).
- **direct_leak is undecided** ("error") for 186, 98, 129 and 371 instructions (O0, Os, Os_clang, O3), nearly all because the faulted run crashes.
- **Where the leaks are.** Os: `poly_decompose` 4, and 2 each in `poly_add`, `polyvecl_add`, `polyveck_add`, `polyveck_decompose` and `polyveck_sub`, and 1 in `poly_sub`. Os_clang: `poly_add` 2, `poly_sub` 1. O3: `polyveck_add` 68, `polyvecl_add` 66, `polyveck_sub` 65, `polyveck_decompose` 23, `poly_decompose` 12, `poly_add` 1, `poly_sub` 1.

## Agreement with the IR report
A row is reproduced if any instruction of the row's own function that maps to its IR instruction gets the same verdict. Rows of `signature_internal` (150) and rows with no mapped instruction are left out, as are rows whose instructions never ran. `results/stats.csv` has every set.

| | O0 | Os | O3 |
|---|---|---|---|
| rows compared | 165 | 115 | 107 |
| agree | 135 (82%) | 101 (88%) | 84 (79%) |
| always "no" | 125 (76%) | 93 (81%) | 90 (84%) |
| random instructions, same function (mean of 2,000) | 135.1 | 97.8 | 52.7 |
| P(random ≥ observed): agreement / kappa | 0.61 / 0.09 | 0.34 / 0.24 | 0.00 / 0.00 |
| precision / recall | 0.59 / 0.83 | 0.72 / 0.59 | 0.36 / 0.47 |
| kappa | 0.56 | 0.58 | 0.28 |
| IR "yes" rows reproduced, of those compared | 33 / 40 | 13 / 22 | 8 / 17 |
| IR "yes" rows reproduced, of all 93 | 33 | 13 | 8 |
| by IR site (any model at the site) | 86 / 96 | 37 / 46 | 29 / 38 |

**By fault type**

| | O0 | Os | O3 |
|---|---|---|---|
| call skips: agree (always "no") | 66 / 70 (40) | 17 / 20 (8) | 11 / 12 (5) |
| call skips: IR "yes" reproduced | 29 / 30 | 11 / 12 | 6 / 7 |
| value faults: agree (always "no") | 69 / 95 (85) | 84 / 95 (85) | 73 / 95 (85) |
| value faults: IR "yes" reproduced | 4 / 10 | 2 / 10 | 2 / 10 |
| without `*uniform*`: agree (always "no") | 119 / 147 (121) | 89 / 101 (90) | 78 / 101 (90) |
| direct_leak (7 rows): IR "yes" reproduced | 0 / 2 | 2 / 2 | 1 / 2 |

- **At Os and O3 every reproduced call skip (11 and 6) is in a `*uniform*` function.** The other call sites are unmapped or were inlined away. At O0, 15 of the 29 are elsewhere.
- **At O0, 23 IR "no" rows come out "yes".** Examples are the six ineffective/correction rows of the other models at the one `poly_add` instruction (below), and six in `poly_sub`.
- **Rows are not independent.** The report's `opA`, `opB`, `undef` and `zero` faults at one instruction share the same machine instructions.

**One case, `poly_add` line 8.** At O0 the skipped `add r1, r3` leaves `a[i]` in `r1`, which is stored, the same as the IR model `opA`. `opB` leaks the secret `b` in the IR study but not here. At Os, skipping `add r3, ip` leaves `b[i]`, the same as `opB`, so the secret leaks directly while the ineffective and correction verdicts of `opA` do not reproduce. Which operand survives depends on the register allocation, so no single skip can match all four models.

## Time
| | IR-level | O0 | Os | Os_clang | O3 |
|---|---|---|---|---|---|
| sequential-equivalent seconds, 31 functions | 18,639 | 39,310 | 5,075 | 5,223 | 16,726 |
| faults or instructions | 83 | 964 | 704 | 588 | 9,678 |
| seconds elapsed on 32 cores | | 2,336 | 421 | 1,028 | 542 |

- **Only indicative.** The IR-level seconds are from another, unknown machine and one fault per ELF. Ours were measured with 32 processes on a 32-core/64-thread Xeon Silver 4314.
- **The slow instructions set the O0 time.** 30 of 964 instructions take over 300 s and 72% of the total. They are mostly runaway loops or returns that run on until they crash; the longest is 2,247 s (`pop {r7, pc}` in `polyvec_matrix_pointwise_montgomery`).
- **Example:** `poly_add` takes 46 s for 25 instructions at O0 (1.9 s each), against 197 s per IR fault in the IR-level timing.

## Limits
- **Coverage.** 31 of 39 config functions. `signature_internal` was not swept and holds 53 of the 93 IR "yes" rows; an older version of this tool reproduced 2 of those 53 on a reduced sweep. Six functions have no input sample. `open` is excluded: its secret buffer has no effect on its output, so no secret was ever applied. The function list comes from the IR study, and covers 12.9% of the IR functions' code bytes at O0 (measured by the bias review, before `open` was excluded).
- **The tests can fire by chance.** The bias review (`reviews/REVIEW_BIAS.md`, on an earlier version) fed unrelated random outputs to the detectors: ineffective and correction fired in 10 of 10 runs for `uniform_eta`, `uniform_gamma1` and hint outputs. It also found that 58% of the O0 correction hits remain with no secret planted. QEMU-test has the same property. These controls have not been re-run on the final code.
- **One position, one execution.** Only the secret's first coefficient and the first execution are tested. The review found about three times as many O3 detections when several positions were combined.
- **Registers start at zero.** An uninitialised register reads 0, where QEMU-test's runs hold whatever earlier code left. The argument layout is rebuilt, because the witness files are not available.
- **Timeouts are wall-clock.** The 300 s limit depends on server load. O3 has 4 timeouts, all `pop` instructions in `*uniform*` functions.
- **Not run:** the generated `main()` of QEMU-test, `signature_internal`, and hardware counters (`perf` was not available).

## History
Earlier numbers were wrong. Two independent reviews by AI agents (`reviews/`) found that skips inside IT blocks did nothing, that register and memory state leaked between trials, and that report rows counted instructions tested under another function's inputs. They also found a vacuous sweep (`open`) and a test with no baseline. These were fixed and everything was run again. Compared with the old results on the same instructions, 3 gained and 3 lost at O0, 19 and 0 at Os, 8 and 1 for clang Os, and 47 and 4 at O3. At O0, agreement went from 137/167 to 135/165; the two rows no longer counted have instructions that never executed.
