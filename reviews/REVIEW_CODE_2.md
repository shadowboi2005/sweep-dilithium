# Second code review: correctness

All runs used a server copy of the frozen snapshot (unicorn 2.1.4, capstone 5.0.7), at most 16 workers, apart from two short overlaps noted at the end. The scripts are next to this file.
**CONFIRMED** = reproduced, or proven from the code and data. **PLAUSIBLE** = mechanism only.
Part A (findings 1-13) was written from code and experiments before `reviews/` was opened. Part B compares with the earlier review.

## Summary

| # | Severity | Where | Finding | Status | Effect on the published results |
|---|---|---|---|---|---|
| 1 | High | emu.py:408 (called at find_faults.py:113) | `Skip` reads the instruction size from memory the previous trial left dirty, so it can skip the wrong number of bytes. Results depend on scheduling and on `--jobs` | CONFIRMED | 2 published O0 rows were produced with the wrong size. Verdicts unchanged. `--jobs 1` gives a wrong row |
| 2 | Medium | tests/ | 45 of 59 mutations survive. `find_faults`, `detectors`, `qemu_inputs` and `ir` have no test, and most fixes from the first review have none | CONFIRMED | None directly |
| 3 | Medium | correlate.py:269-280 | The random-PC null is described as "executed PCs" but samples all PCs, including never-executed and all-crash ones. The observed rows exclude those | CONFIRMED | O0 P(kappa) 0.088 becomes 0.12 or 0.27. Conclusions hold |
| 4 | Low-medium | correlate.py:99-103 | `correlation.csv` `agree` ignores the new "undecided / not executed" rule and includes inlined copies, so it contradicts `report_rows.csv` | CONFIRMED | 48 of 303 O0, 38 of 9,926 O3 and 2 of 268 Os comparable rows; 10 O0 "agree" rows never ran. REPORT.md uses report_rows, so it is unaffected |
| 5 | Low (latent) | emu.py:442 | IT block left through a taken branch or return: the NOP stays for the rest of the call, so the skip is not "first execution only" | CONFIRMED | None: no swept block has this shape |
| 6 | Low | emu.py:376 | An early stop without an exception (e.g. `wfi`) is reported as a timeout and aborts the instruction | CONFIRMED | None |
| 7 | Low | find_faults.py:171 | `cpu_seconds` includes Unicorn's timeout thread | CONFIRMED | 7-13% too high in all builds |
| 8 | Low | emu.py:401, 248-256 | `emu_insns` over-counts the rest of the block at each skip | CONFIRMED | Those columns only |
| 9 | Low | find_faults.py:278-280 | Checkpoint key omits `MBC_PREFER_CALLER`, which selects the layout | CONFIRMED (rows reused) | None |
| 10 | Low | correlate.py | Crashes on empty or degenerate input; builds with the same directory name silently overwrite each other; an ELF that does not match the faults is accepted | CONFIRMED | None |
| 11 | Low | correlate.py:212 | `elapsed_seconds` columns are never filled | CONFIRMED | 4 empty columns in timing_compare.csv |
| 12 | Low | find_faults.py:227-229 | Active-length check raises `KeyError` when the secret buffer failed calibration and `--secret-pos >= 1` | CONFIRMED (code) | None at pos 0 |
| 13 | Low | emu.py:266, 323, 190 | Small emulator issues | CONFIRMED | None |

**Reproduction of the published CSVs.** All comparable columns (everything except `seconds` and `cpu_seconds`) were compared exactly:

| build | rows re-run | differ |
|---|---|---|
| Os | 704 (all 31 functions) | 0 |
| O3 | 3,637 (18 functions, including the direct-leak ones) | 0 |
| Os_clang | 270 (18 functions) | 0 |
| O0 | 368 + 232 (23 functions, 2 of them in both sets) | **2**, both from finding 1 |

`correlation.csv`, `report_rows.csv` (O0, Os, O3), `opt_compare.csv`, `stats.csv` and `timing_compare.csv` regenerate **byte for byte**. Os `layouts.json` is also byte-identical, and the Os timing counts (n_pcs, trials, emu_insns) are equal.

---

## 1. Wrong instruction size from dirty memory (High, CONFIRMED)

**Where.** `emu.py:408`: `self.size = machine.disasm_one(pc)[1] or 2` reads *live* guest memory. `_pc` builds `Skip` (find_faults.py:113) before the first `restore_memory()`. Memory is restored at the start of a trial, not at the end, so the machine still holds whatever the previous instruction's last trial wrote.

**Scenario.** At O0, skipping the `str r0, [sp, #..]` that saves the output pointer makes the function write its output through a null pointer. Addresses 0 to 0xfff are then overwritten, and that range contains the function's own code (0xc54 to 0x1004). The next instruction this worker handles *for the same function* gets its size from garbage:
- 2 read as 4: two instructions are skipped;
- 4 read as 2: execution resumes in the middle of a `bl`.

The bytes are restored before the trial runs, so nothing else shows the error. `_on_pc` simply adds the wrong size.

**Reach** (`dirty_size.py`, one trial per instruction plus the fallback trial; pairs X -> Y where X leaves Y mis-sized):
- O0: 14 functions (polyvecl/polyveck functions, the matrix function, poly_uniform_gamma1);
- Os_clang: poly_uniform_eta and poly_uniform_gamma1;
- O3: polyvecl_uniform_gamma1;
- Os: none.

With `--jobs 1` the instruction right after X is hit in O0 polyvecl_pointwise_poly_montgomery (0xe38 -> 0xe3a), Os_clang poly_uniform_gamma1 (0x1b76 -> 0x1b78) and O3 polyvecl_uniform_gamma1 (0x10044). `--jobs 1` is what the README recommends for the like-for-like timing.

**Wrong row** (unmodified tool; `run_logged.py` only logs the size, or corrects it in the second run; `--jobs 1 --seeds 0 1 --field-mod 3`, polyvecl_pointwise_poly_montgomery, 0xe3a `movs r0, #0`):

| | fired | sv_examined | crash | direct_leak | n_trace_pairs |
|---|---|---|---|---|---|
| tool (size 4) | 6 | 3 | 0 | no | 6 |
| correct (size 2) | 3 | 0 | 3 | error | 0 |

The published full run has the correct (crash) version of this row.

**Published rows affected.** `emu_insns` identifies which size was used. Each row was run fresh, after X, and after X with the size forced to 2 (`order_effect.py`, full parameters):

| O0 row | fresh | after X | after X, size forced to 2 | published |
|---|---|---|---|---|
| polyveck_caddq 0xf2e `b` | 24,231,936 | 24,230,912 | 24,231,936 | **24,230,912** |
| polyveck_shiftl 0xfde `b` | 13,742,080 | 13,741,056 | 13,742,080 | **13,741,056** |

Both published rows therefore skipped two instructions. The extra one is `ldr r0, [sp, #12]`, which reloads the value already in r0, so all verdicts are unchanged and neither PC has a report row. My unpatched 12-job run hit caddq 0xf2e and not shiftl 0xfde. With a size-correcting wrapper the 232 re-run O0 rows differ from the published ones only at 0xf2e. Instructions slower than 30 s (O0) or 60 s (Os_clang, O3) were not re-run.

**Tests.** The mutation "skip size always 2" also survives (finding 2).
**Fix.** Take the size from `function_insns` (pristine), or call `machine.restore_memory()` before reading it. Better, restore after each instruction. Add a test: dirty the code, build the Skip, check `size`. Re-run at least the two O0 rows.

## 2. The tests do not cover most of the fixes (Medium, CONFIRMED)

59 one-line mutations (`mutate.py`): **14 killed, 45 survive** (details below). The 13 existing tests pass and are meaningful for what they check. No test imports `find_faults`, `detectors`, `qemu_inputs` or the DWARF part of `ir`. Every Skip test runs a single trial, and the machine is always new or clean.

## 3. The random-PC null is not the null it says it is (Medium, CONFIRMED)

correlate.py:269 says "as many random **executed** PCs of the same function". `by_fn` holds every PC (271-272): never executed, and crashed on every value (both always "no"). The observed side keeps only rows whose PCs executed and reached a verdict (`agree` in yes/no). The two sides are not drawn from the same population. Recomputed with the same seed and 2,000 draws (`stats2.py`; row 1 equals stats.csv):

| | O0 mean agree / P(agree) / P(kappa) | Os | O3 |
|---|---|---|---|
| published (all PCs) | 135.1 / 0.605 / 0.088 | 97.8 / 0.338 / 0.242 | 52.7 / 0 / 0 |
| executed only (as documented) | 135.9 / 0.655 / 0.124 | 98.0 / 0.352 / 0.270 | 52.2 / 0 / 0 |
| executed and sv_examined > 0 (the observed rule) | 137.4 / 0.735 / **0.272** | 97.6 / 0.326 / 0.327 | 51.8 / 0 / 0 |

The published version makes the mapping look slightly better than chance in kappa at O0. With the matching population that disappears. The report's wording ("about as often as the mapped ones") is supported, more strongly. P = 0 of 2,000 should be reported as < 0.0005.
**Fix:** filter `by_fn` with the `_decided` rule and say so, or correct the comment and REPORT.md.

## 4. correlation.csv disagrees with report_rows.csv (Low-medium, CONFIRMED)

`cmd_map` line 102 sets `agree = _agree(ir, f[test])` without `_decided`. A PC that never executed, or that crashed on every secret value, has `no` in the CSV and is written as `agree=yes` against an IR "no" (or `agree=no` against an IR "yes").
- All-crash ine/cor rows with an agree value: O0 48 of 303, O3 38 of 9,926, Os 2 of 268.
- Never executed but `agree=yes`: O0 10.
- `kind=matched` also includes copies inlined into other functions: Os 101 rows, O3 4,249 rows. There `agree` compares with a test run for another function. `swept_function` and `ir_function` do show it.

REPORT.md and stats.csv use report_rows.csv, so no published figure is wrong. A reader counting correlation.csv gets a different answer.

## 5. NOP stays when an IT block is left early (Low, latent, CONFIRMED)

The bytes go back only when the instruction *after* the block is reached (`_on_it_end`). If a later member of the block is a taken branch or return, that address is never reached and the NOP stays until the call returns.
**Reproduction** (`skip_edge.py`): `movs r0,#0; L: subs r2,#1; itt ne; addne r0,#1; bne L; bx lr` with r2=5. Unfaulted r0=4. Skipping `addne` once must give 3, the tool returns **0** (fired=1).
**Scope.** Every IT block of the swept functions was checked (`it_scan.py`). The 10 blocks with a branch or return (all Os) are single-member blocks where the return is itself the skipped instruction, which works (tested). No published row is affected.
**Fix:** also stop at the branch target. Simplest: at the *next* arrival at the patched PC or the IT instruction (hits > 0 and still patched), stop, restore and resume. Or refuse blocks with a non-fall-through member after the skipped one.

## 6. Early stops are "timeouts" (Low, CONFIRMED)

`wfi` in a guest function: `EmulationTimeout` after 0 s with `timeout_s=5`. QEMU-test (unicorn_runner.py step 5) raises TrialTimeout only if at least 99% of the limit has elapsed, and otherwise reads the outputs. One such trial aborts the whole instruction as `timeout`. The 4 published timeouts (all O3) ran 300 s or more, so there is no effect. Fix: check the elapsed time; otherwise treat as a crash, or as QEMU-test does.

## 7. cpu_seconds is not CPU time of the emulation (Low, CONFIRMED)

`process_time()` counts all threads, and `emu_start(timeout=...)` runs a polling thread. A long single call used 129.0 s CPU in 115.8 s wall (equal without a timeout). In the CSVs, CPU is 1.13 (O0), 1.07 (Os), 1.13 (Os_clang) and 1.10 (O3) times wall, and exceeds wall on 83-99% of rows. The timeout also costs about 0.2 ms per call (5,000 empty calls: 1.03 s against 0.11 s), which is in `seconds`. Document it, or use thread CPU time.
The golden-timing accounting is otherwise right. `_golden` starts its clock after building the inputs, so input sampling is not included.

## 8. emu_insns (Low, CONFIRMED; already in the first review)

Per-trial counts in poly_add O0 still fall 4115, 4114, 4113, 4112 for consecutive straight-line skips: moving PC re-enters the block and it is counted again.

## 9. Checkpoint key (Low, CONFIRMED)

`MBC_PREFER_CALLER` changes `pick_call_site` (qemu_inputs.py:351) and so the layouts, but is not in the key. With it set, a finished run reported "1 done / 10 done" and reused the rows, while layouts.json is rewritten from the new layout. Whether the layout of those two functions changes was not tested. Everything else that matters is in the key (ELF, IR, config, samples, witness files, tool sources, library versions, all sweep options).

## 10. correlate.py on unusual input (Low, CONFIRMED, `edge_corr.py`)

- `stats` with no comparable row: `KeyError: 'agree'` (283).
- `stats` where kappa is undefined in a draw (only IR-"no" rows): `TypeError` at `sum(kappa)`, because `_score` returns `""`.
- `stats` and a non-existing `--out`: `FileNotFoundError` (the only subcommand without `makedirs`).
- `compare` with empty rows, and `map` with an empty report: `IndexError`.
- `compare` and `timing`: the label is the directory name. `x/O0` and `y/O0` silently give one column set.
- `map` with the Os faults and the O0 ELF runs without a warning and yields "unmapped" rows.

## 11. Dead columns (Low, CONFIRMED)

`cmd_timing` copies `elapsed_seconds`, which find_faults never writes. All four `*_elapsed_seconds` columns in results/timing_compare.csv are empty. REPORT.md's "seconds elapsed on 32 cores" therefore cannot be checked from the CSVs, although the report says all figures come from them.

## 12. Active-length check (Low)

`active.get(secret, 1) <= secret_pos` followed by `active[secret]` in the message: a secret whose calibration failed is absent from `active`. QEMU-test then fills the whole buffer, so the sweep would be valid; with `--secret-pos >= 1` the tool reports `error: KeyError`. At the default position it is correct. `open` is excluded as intended.

## 13. Small emulator issues (Low, CONFIRMED)

- emu.py:266: "other semihosting calls: ignored" writes PC without the Thumb bit, so the call crashes with an invalid instruction instead of being ignored (`bk.py`).
- emu.py:322-323: a runaway into `randombytes` with r1 = 0xffffffff builds a 4 GB string (15 s, 4.5 GB RSS). It then ends as a normal crash, so no worker exception, but 32 workers could exhaust memory. Check the range before `stream.read`.
- `_pc` has no `try/finally` around `skip.detach()`, and any other Python exception in a worker aborts the sweep (resumable).
- `Machine` never closes its four memfds (leak of 4 descriptors per Machine). The count stays below 1,024 for the 39 functions.
- `layouts.json` is merged with an older file even when the key changed. A `--functions` subset run overwrites the CSVs with only that subset.

---

## Mutation tests (`mutate.py`)

**Killed (14).** IT patch off; IT members treated as plain; no stop/resume at the block end; bytes not put back; every execution skipped; no `context_restore`; no `MADV_DONTNEED`; flash only; all but flash; `it_block_ends` ignoring "else" slots, or ending one member early; `_decided` ignoring `fired`, or ignoring `sv_examined`; wrong kappa.

**Survive (45).**

| file | mutations |
|---|---|
| emu.py (16) | resume condition ignores `fired`; `begin()` or `end()` without a TB flush; no `skip.end()` in `finally`; 32-bit member replaced by two 16-bit NOPs; **size always 2**; resume without a timeout; no timeout at all; `disarm()` a no-op; `reset()` does not re-arm; `detach()` leaves hooks and `_skip`; **code-changed check removed; stray-block check removed; never flush**; peripherals not restored; timeout reported as a crash |
| find_faults.py (9) | active-length check removed; `restore=False` for faulted runs; `skip.reset()` dropped; `it_ends` not passed to Skip; crash does not end the secret value; fallback reference run not disarmed; **checkpoint key ignored; golden record ignored**; timeout verdict dropped |
| detectors.py (6) | alpha = 0 accepted; ineffective over any draw; ineffective over any seed; constant accepted as direct leak; trace pairs not shuffled; mod-sub as xor |
| qemu_inputs.py (4) | one-byte override; eph draws from the main RNG; randombytes stream not reset; active lengths ignored |
| ir.py (2) | line off by one; inlined owner ignored |
| correlate.py (8) | **own-function rule removed; "not executed" or "undecided" counted as "no"**; precision wrong; null uses one PC; null grouped by fault; site collapsing uses min; IR timeout compared as a verdict |

Reverting these first-review fixes passes the suite: own-function rule (2), checkpoint key (3), stale translations (part of 6), golden timing (7), `open` (8), fallback (9), the `mc_any` categories in `cmd_map` (10). Reverting the IT fix (1), the CPU state (5) and the memory restore itself (6) is caught.

Cheapest additions: several trials on one Skip (reset, disarm); a 32-bit and a branch-terminated IT block; code in SRAM that differs between trials; `Skip` built on dirty code; a synthetic `cmd_map` and `cmd_stats` run; one small `find_faults` run with resume compared with a stored CSV; detector cases with known verdicts.

## Part B: status of the first review's findings

| # | Finding | Status | Evidence |
|---|---|---|---|
| 1 | IT members not skipped | **Fixed** (latent gap: finding 5) | `it_indep.py`: all 22 + 5 + 180 members, 3 consecutive trials each, against an independent NOP method: fired, outcome and outputs identical (391 fired trials). 32-bit members, else slots, back-to-back blocks, flags after resume, conditional returns, crash and timeout after the resume all correct |
| 2 | Foreign PCs in `mc_any` | **Fixed** in report_rows; `kind=matched` rows still include them (finding 4) | own-function filter; CSVs regenerate; no test |
| 3 | Stale checkpoints | **Fixed**, except an environment variable (finding 9) | key read; kill -9 in mid-run and resume gave identical rows |
| 4 | README fault-model text | **Fixed** | README caveat now says first iteration |
| 5 | IT state leak | **Fixed** | `context_restore`; also checked VFP s0, FPSCR, CONTROL, PRIMASK, BASEPRI |
| 6 | Memory not restored | **Fixed for trials**; one residual order dependence (finding 1) | generated code in SRAM, harness-written code and self-modified flash all behave; 4,900+ re-run rows identical |
| 7 | Golden time lost on resume | **Fixed** | resume test: golden_seconds kept for finished functions, over several runs. `cpu_seconds` now uses process time but is still above wall (finding 7); `elapsed_seconds` still listed (finding 11) |
| 8 | `open` vacuous | **Fixed** | error row in all builds; README states the renaming |
| 9 | Fallback pairs | **Fixed** | 4 pairs, "error", reference run disarmed, same seeds and eph strings as `collect_pairs`. Remaining: QEMU-test's 120 s per-ELF direct-leak limit is not ported (only undecided verdicts are involved) |
| 10 | `mc_any` coercion | **Fixed** in report_rows/stats, **open** in correlation.csv | finding 4 |
| 11 | `emu_insns` | **Open** | finding 8 |
| 12 | Non-error stop = timeout | **Open**, now CONFIRMED with `wfi` | finding 6 |
| 13 | Prologue aliases an IR index | **Open, latent** | `prologue.py`: only non-swept functions qualify (KeccakF1600, keypair, pack_pk, pack_sk, ...). No swept PC is affected |
| 14 | Small items | **Mostly open** | subset overwrite; duplicate report row (316 rows in opt_compare); `.text` mismatch only warns; `link()` hides stderr; `build_os_clang.sh` links stale objects; no try/finally. The truncated-checkpoint-line problem is fixed |

## Checked and found correct

- **Skip outside IT blocks**, the `IT` instruction itself, conditional returns, member whose condition fails, first-execution counting, detach and exception paths, timeout left for the resume (1.0 s total for a 1 s limit).
- **Linear disassembly:** every swept PC is an objdump instruction start, no instruction is missed, and no literal pool lies inside a swept range (all four builds). So no literal is decoded as IT.
- **restore_memory:** private mappings, harness-written and guest-written code, code changes in flash, execution from SRAM.
- **QEMU-test fidelity:** `domain_values`, `infer_field_mod` and samplers identical on 1,458 cases (54 distribution strings, lengths 0 to 4096). Read line by line and equal: early-stop sweep, tracker, stop rule, crash/timeout handling, trace-pair directories and order, direct-leak checks and messages, calibration, override rule, `derive_all`, probe mode.
- **DWARF mapping:** PC -> (function, index) equals `addr2line` for all 964 + 704 + 9,678 PCs. The only differences are prologue lines (tool says none, correctly) and one PC where addr2line reports no line and the line table agrees with the tool.
- **Call skips:** in report_rows the `bl`/`b` alone gives the same verdict as "any mapped PC" in 69/70, 20/20 and 12/12 rows. The exception is poly_decompose ineffective at O0, IR "no".
- **REPORT.md tables:** counts per build, direct-leak locations, errors, agreement, baselines, call/value split, "not executed", 30 slow instructions with 72% of the time, 83 IR faults and total seconds all match the CSVs. Per-function timing sums, n_pcs, trials and emu_insns in timing_dilithium.csv match machine_faults.csv.
- **Resume:** finished functions, partial runs, kill -9, a different function list in the same directory. `todo` ordering and golden reruns are right.
- **firmware/:** `build_elf.py -h` works from the new location, paths and imports are right, the shell script parses, and all the C files it names exist.

## Not checked

- Building the ELFs (not allowed or not available here). The relabelled IR is reproduced exactly by `ir.relabel`, the ELFs were not. `build_os_clang.sh` was only syntax-checked (`bash -n`), because a run without arguments was refused by the sandbox.
- O0 instructions slower than 30 s in ten functions, the remaining O0 functions (invntt, poly_uniform*, polyveck/polyvecl invntt and others), and the remaining O3 and Os_clang functions.
- Which predecessor each published O0 worker had, so "2 rows" covers the re-run rows only.
- The rebuilt layouts against real witness files; the README's "138 cases"; the History numbers; elapsed times.
- The server was shared. Two of my jobs briefly overlapped (8 + 12 requested workers, about 14 active). One timing figure (finding 7) was measured on a loaded machine, and only its CPU/wall ratio is used.
