# Adversarial review: bias, leakage, overfitting

Line numbers refer to the frozen snapshot. Local scripts `a1.py`..`a11.py`, the server driver `drv.py` and run scripts `runA.sh`..`runE.sh`, and their CSVs/logs are next to this file. `drv.py` imports the submission's modules unmodified and changes one thing at a time (it uses a 60 s instead of 300 s trial timeout).

## Verdict

**The code is blind. The headline number is not evidence of anything.** I found no path from the fault report, fault names, IR fault sites or earlier results into `find_faults.py`, and no hardcoded PCs, opcodes or expected verdicts. The port is real: 30 of 33 O0 IR-yes rows that reproduce give the *same* witness (position, s, alpha) as the IR report.

The proposed headline, "agreement with report rows" (O0 137/167, Os 97/115, O3 82/107), does not survive a baseline check:
- O3 (76.6 %) is **worse** than always answering "no" (84.1 %). O0 and Os beat it by 6.0 and 3.5 points.
- Picking *random PCs of the same function* instead of the mapped ones gives the same agreement at O0 (137.0) and Os (97.2).
- On value faults (binop/load-store, 95 rows in every build), agreement is below always-no in all three builds (69, 80, 71 against 85).

What does survive is narrower: at O0, call-skip faults correspond (68/72, kappa 0.89), largely because skipping a `bl` is the same fault. The "detected instruction" counts are inflated: about half are in hash/sampling functions, where the tests fire on any change of output. They also depend on harness state and on secret position 0.

I found no evidence of deliberate tuning, but the design choices were made with the IR verdicts and an earlier report-driven version of the tool in hand (finding 10).

---

## Findings, most severe first

### 1. (Critical) Agreement does not beat trivial baselines; the metric is wrong
`a1.py`, `a3.py`, `a8.py`, from `results/*/report_rows.csv`.

| build | n | TP | FN | FP | TN | agree | always-no | precision | recall | kappa |
|---|---|---|---|---|---|---|---|---|---|---|
| O0 | 167 | 33 | 7 | 23 | 104 | 0.820 | 0.760 | 0.59 | 0.82 | 0.57 |
| Os | 115 | 17 | 5 | 13 | 80 | 0.843 | 0.809 | 0.57 | 0.77 | 0.56 |
| O3 | 107 | 14 | 3 | 22 | 68 | **0.766** | **0.841** | 0.39 | 0.82 | 0.40 |

Per test (agree, always-no, kappa):

| | ineffective | correction | direct_leak (7 rows) |
|---|---|---|---|
| O0 | .800, .700, .57 | .850, .825, .60 | 5/7, TP 0 of 2 |
| Os | .870, .778, .66 | .889, .852, .60 | 2/7, kappa -0.21 |
| O3 | .800, .820, .46 | .780, **.880**, .37 | 3/7, kappa 0.13 |

Null models (2000 draws; same number of random PCs from the same swept functions, "any" rule):

| | observed agree | null mean agree | P(null >= obs) | kappa obs / null mean / 95 % | P(kappa) |
|---|---|---|---|---|---|
| O0 | 137 | 137.0 | 0.60 | .566 / .444 / .596 | 0.084 |
| Os | 97 | 97.2 | 0.61 | .556 / .512 / .661 | 0.36 |
| O3 | 82 | 45.5 | 0.00 | .398 / .089 / .147 | 0.00 |

O3 beats its null only because random O3 PCs give far more false "yes". It is still below always-no. Permuting sites *within* a function gives a mean of 134.7 (O0, P 0.43), 101.6 (Os, P 1.0) and 84.7 (O3, P 0.91). Much of that permutation is fixed, because many functions have one site, so treat it as support only.

By fault family:

| | fnSkip: agree / n, kappa (always-no) | binop + load/store: agree / n, kappa (always-no) |
|---|---|---|
| O0 | 68/72, .89 (42) | **69/95**, .10 (85) |
| Os | 17/20, .68 (8) | **80/95**, .36 (85) |
| O3 | 11/12, .83 (5) | **71/95**, .29 (85) |

O0 fnSkip is well above its null (55.0, P < 0.001). Os fnSkip (P 0.41) and O3 fnSkip (P 1.0) are not. At O0, 29 of 33 TPs are fnSkip. Skipping the `bl` is the same fault as removing the call, so this validates the port rather than testing "survival" of a different fault.

**Fix.** Drop "agreement" as headline. Report the 2x2 table, precision, recall and kappa, always next to always-no and the random-PC null, split into call skips and value faults. Suggested wording: "Call-skip findings carry over at O0 (29/30 IR-positive rows). For value faults, PC-level agreement is no better than always answering no."

### 2. (Critical) The 167/115/107 rows are not independent, and dropped rows flatter the result
- Rows for different IR models (opA/opB/undef/zero) at one IR site get the *same* PC set and so the same machine verdict. Distinct (PC set, test) pairs: **98 / 46 / 38**, at 50 / 24 / 20 sites. In 7 site-tests per build the IR models disagree with each other, so a single skip cannot agree with all of them.
- Site-collapsed (IR "yes" if any model is yes): O0 88/98 (kappa .79), Os 38/46 (.64), O3 32/38 (.67). This is the more favourable *and* more honest number.
- 78 of 167 O0 rows (68 of 115 Os, 68 of 107 O3) are in functions with **no** IR-yes row. They agree 70, 68 and 60 times. On the remaining rows: O0 67/89 (always-no 49), Os 29/47 (25), O3 **22/39** (22, i.e. equal to always-no).
- All 150 rows of `signature_internal` (53 of 93 IR-yes, all 30 IR timeouts) are "not swept" and leave the denominator. `_agree` returns "-" (`correlate.py:59`), and another 52 (Os) and 60 (O3) rows are "unmapped". Of **all 93 IR-yes rows, 33 (35 %) reproduce at O0, 17 (18 %) at Os, 14 (15 %) at O3**.
- The predecessor tool *did* run signature_internal (`targeted_unicorn/results/O0/summary.md:64`, reduced 4x4 sweep): **2 of 53** IR-yes rows reproduced. Excluding it on cost is defensible (the README says "many hours"), but the earlier poor result must be disclosed. Over all 317 rows that run agreed on 191 (60 %).

**Fix.** Use sites as the unit. Give a "reproduced / all IR-yes" column, with unmapped and not-swept as their own outcomes, and state the earlier signature_internal result.

### 3. (Critical) ineffective/correction fire on any change of output in the hash and sampling functions
`a5.py` feeds the submission's own `EarlyStopSweep` (`tests.py:71`) with *random, unrelated* outputs: golden and faulty are independent draws of the output distribution, so there is no secret anywhere.

| output type | ineffective | correction |
|---|---|---|
| uniform_eta bytes, 256 or 1024 coeffs | 10/10 yes | 10/10 yes (2 secret values needed) |
| uniform_gamma1 bytes | 10/10 | 10/10 |
| same, only **one** coefficient changed | 10/10 | 10/10 |
| poly_uniform bytes | 10/10 | 0/10 |
| hint 0/1, 256 coeffs | 10/10 | 10/10 |
| w1 in [0,44) | 10/10 | 5/10 |
| int32 mod q (NTT etc.) | 0/10 | 0/10 |

Cause: those outputs are compared byte-wise (`qemu_inputs.py:542-544`). With high bytes in {0x00, 0xFF}, 4 seeds, 256 tries and 1000+ positions, a chance hit is certain. Nothing corrects for multiplicity. The IR tool has the same flaw, so *both* sides say "yes" and "agree".

Effect:
- Detected PCs in the `*uniform*` functions: **82/164 (O0), 64/171 (Os), 838/1440 (O3), 58/110 (Os_clang)**.
- These functions supply 14/33, 11/17 and 6/14 of the headline TPs. Without them: O0 119/147 (always-no 121), Os 85/101 (90), O3 76/101 (90). **Agreement is below always-no in every build.**
- Os correction in the XOF functions (41 PCs, seeds 0..15 instead of 0..3): 19 of 37 "yes" verdicts disappear. Ineffective stays 41/41, as the null predicts, because it only needs some pair per seed.

`poly_make_hint` and the w1 outputs of `*decompose` are exposed to the same effect when a fault touches many coefficients. Their actual hits are mostly at position 0, so they are not necessarily chance.

**Fix.** Report a null-control column (random-oracle fault) and exclude, or separately label, functions where the control fires.

### 4. (High) About half of the correction hits (O0 58 %, Os 55 %, O3 44 %) survive with no secret planted
Placebo run (`runC.sh`): same sweep, **no secret planted** (no override, one grid). Correction "yes" that remain: **O0 68/118, Os 53/96, O3 308/707**. In the non-XOF functions: O0 13/51, Os 17/51, O3 141/257 (`polyvecl_add`, `poly_make_hint`, `open`, and at O3 `ntt` 29 and `invntt_tomont` 40). With the ported definition "exists s, alpha != 0", any constant output change counts. (The ineffective test cannot fire in this placebo by construction, so it says nothing about that test.)

Concrete cases:
- `open` is swept with no secret at all. Calibration gives `sm: 0, pk: 0` (`results/O0/layouts.json`), so `sm` is all zero and `apply_override` returns unchanged (`qemu_inputs.py:216`). All "256 secret values" are identical. Its one hit per build (O0 `0xbe2 movs r1,#0`, "alpha=175") cannot be secret-related. The pinned QEMU-test config marks this entry `open(calibrate error)`; `config/dilithium.json` silently renamed it to `open`.
- `poly_make_hint`: calibration gives `a1: 0` and `a0: 1023`, so a1 is always zero and is not a background.

**Fix.** Require alpha to vary with s, or at least report it. Run the placebo as a standard control. Drop `open`.

### 5. (High) Results depend on harness state, and the independence claim is false
`Harness` says trials are "independent of their order" (`qemu_inputs.py:551-553`). `Machine.call` sets only r0-r3, sp and lr (`emu.py:299-302`). **r4-r12 and the flags persist from the previous trial or crash**, in whichever worker ran it. QEMU-test starts a fresh machine per trial (`unicorn_runner.py run_trial`, `Machine.from_elf`).

Os, 13 functions, 259 PCs, *identical code*, 3 runs (published, rerun, shuffled queue): **12 PCs change some verdict; 5 change ineffective/correction** (about 7 % of the 73 PCs detected in any run). Example: `poly_decompose 0x1918 movw ip,#0x2c0b` is no/no, yes/yes, no/no, depending on whether `ip` still holds the constant from the previous run. The author's own rerun on the server (`sweep_pyemu_fault/out/Os`) differs from `results/Os` at 2 of 69 PCs, both direct_leak. O0: verdicts reproduce for ineffective/correction on 257 PCs (2 direct_leak changes), and the author's O0, O3 and Os_clang reruns show no verdict differences on the PCs they cover.
With fixed registers or other fills: r4-r12=0x20100000 changes 21 of 259 PCs (6 ineffective "yes" and 1 correction "yes" lost).

O0, 8 functions, 42 detected PCs: filling **output buffers** with stale random data instead of zero (`qemu_inputs.py:643`) removes 11 PCs (e.g. `poly_pointwise_montgomery 0x1394 str.w`: "ineffective at s=0" only because the buffer starts at the correct answer 0). Stale stack 0xA5 removes 10. Together **18 of 42 (43 %)** depend on zeroed memory. Report-row agreement does *not* move (55/75 in all three), because those PCs are mostly not mapped to report rows.

Worked example: `poly_add` O0 `0x12a6 str r2,[sp,#4]`, correction, s=0, alpha=3661889. The skipped spill leaves the harness-zeroed stack slot (`qemu_inputs.py:629`), so pointer `b` is 0. Word 0 is the vector table's initial SP `0x20400000`, and (0 - 0x20400000) mod q = **3661889** exactly. `poly_sub 0x12e2` (alpha 4718528 = q - 3661889), `polyveck_add 0xf5a` and `polyveck_sub 0xf98` show the same constants. Filling the stack with 0xA5 removes the first two (the other two were not rerun). These hits do need the fixed secret coefficient, so they are not placebo hits; they are harness artefacts.

**Fix.** Set all core registers and flags before every call, fill stack and output buffers with per-seed random data, rerun everything, and report the flips.

### 6. (High) Function list comes from the IR study and covers 12.9 % of the code
- `config/dilithium.json` is the IR study's list (`targeted_unicorn/README.md`: "cleaned up from Dilithium-LLVM/smtTestDone.txt"). All 32 functions with report rows are in it; 31 are swept (all but `signature_internal`).
- Swept symbols: **2410 of 18660 bytes (O0), 14.9 % (Os), 23.8 % (O3)** of the 106 IR functions. Never swept at O0: `KeccakF1600_StatePermute`, `rej_uniform`, `rej_eta`, `montgomery_reduce`, `reduce32`, `caddq`, `poly_chknorm`, packing/unpacking, `sign`/`verify` internals. At O3 the small helpers are inlined into swept functions, so the builds sweep different code and per-build counts are not comparable.
- The README calls the config "the IR-level run's config" (`README.md:73`). The pinned config has 33 entries; this one has 39 (6 more, all erroring with missing samples, plus the renamed `open`).
- 18/164 (O0), 20/171 (Os) and **287/1440 (O3)** detections are in functions on *public* data treated as secret: `poly_uniform` (rho, the matrix A), `polyveck_shiftl` (t1) and `open`. This is QEMU-test's choice, but the report must say it.

**Fix.** Say "blind within functions the IR study already analysed". Give bytes swept, and a detection rate per fired, non-crashing PC.

### 7. (High) hit=1 plus secret-pos 0 measures one matched coefficient
hit=1 (`emu.py:345`) usually faults coefficient 0, which is where the secret is planted.
- O3, 5 functions, 482 PCs: pos 0 detects 44, pos 4/8/12/16 detect 39/40/39/35, common 15/17/16/12 with pos 0, **union 134**. Examples missed at pos 0: `poly_add 0x157e0 ldr.w r6,[r4,#0x3e4]`, `poly_sub 0x158ea sub.w` (the second unroll lane). So O3 undercounts about 3x and the count is set by unrolling.
- O0, 131 PCs: pos 0 detects 31, pos 4 detects 14 and pos 1020 detects 13 (common 11 and 10).
- `README.md:106` says an IR fault "acts every time that instruction runs". This is **wrong** for loops: the generator unrolls fully and faults only the first iteration (`binOp_load_store_Fault.cpp:787-819`, `isFirstIterationBlock`), and the report shows it (`poly_add_line8_opB` changes only bytes [0,1,2]). hit=1 therefore matches the IR model, which favours agreement. It is a legitimate choice but is described backwards.

**Fix.** Sweep several positions or hits, or state the restriction; correct the README caveat.

### 8. (Medium) The "any mapped PC" rule gets easier with more PCs, and mixes experiments
- O3 maps up to 203 PCs per row (97 of 107 rows have >= 10). At O3, *every* mc_any "yes" comes from rows with >= 10 PCs: 14 TP and **22 FP**. Majority rule: O0 122, Os 95, O3 94 (kappa .24/.26/.45). "All PCs" rule: 118/96/90.
- `correlate.py:83` pools PCs of *other* swept functions via inlining, with a different secret and harness. Os: 101 of 268 links; O3: 4249 of 9940. Keeping only the row's own function: Os 100/115 (TP 17->12, FP 13->5), O3 84/107 (TP 14->8). 13 and 14 rows flip. Example: `poly_add line8` (secret `b`) is "reproduced" by 129 direct_leak PCs of `polyvec*_add`/`_sub`.
- 1440 O3 detections are 107 distinct IR sites (O0: 87 sites, Os: 72). Unrolled copies are not new findings.

**Fix.** Report the own-function rule and a per-PC-count-matched null, and count distinct sites.

### 9. (Medium) Denominators: crashes, unfired PCs and "machine-only"
- "No" covers PCs that crashed in all trials (209 / 119 / 435 / 143 for O0 / Os / O3 / Os_clang) and never fired (76 / 65 / 189 / 63; I did not separate dead code from literal-pool words). Detection rates: O0 15.7 % raw, 21.6 % of valid PCs; Os 22.8 -> 30.3 %; O3 14.7 -> 15.6 %. A crash depending on the secret is itself a leak, but it is skipped (`find_faults.py:131-142`).
- For *rows*, the crash effect is small: 2 O0 rows have only vacuous PCs (135/165 without them), and 0 at Os/O3.
- "machine_only" does not mean "missed by the IR study". Only 120/1043 O0 PCs map to any report row, and **43/164** detections (Os 24/171, O3 257/1440). The IR study tested just 133 faults and never attempted most of these PCs. Do not present these as new faults.

### 10. (Medium) Not blind in development, and some claims of sameness are wrong
- An earlier, report-driven version exists in the same project (`targeted_unicorn/README.md`), with hit=1 and the any-rule, and saw 33/93 and 191/317. On the 167 non-signature rows it agreed on **93/167 (56 %)**; after porting QEMU-test's inputs and tests it is 137/167. The change was principled (30/33 identical witnesses support it) and is a port, not row tuning. But the report is the development set; there is no held-out scheme, parameter set or build.
- "Same experiment" (`README.md:15`) fails for at least `poly_ntt`: the IR witness is **s1=12 at pos 916** (`data/fault_report_dilithium.csv:96`), impossible with the submission's domain [-2,2] on int32 words. The IR run used bytes. The submission gets "no" there. `poly_uniform_eta` (`rej_eta`) also has different witness positions (IR 17 and 9, here 449 and 216), so its inputs differ.
- Checkpoint key (`find_faults.py:259`) has no code or config hash, so resumed runs can mix code versions. `results/O0` was resumed: `golden_seconds` is 0.0 for the first functions and `server/O0.log` shows "(25 done)". So O0 timings for those functions understate the cost.

### 11. (Low) Other
- `ineffective` uses raw int32 equality but `correction` uses mod-q deltas (`tests.py:38-39, 90`). Deciding ineffective mod q removes **11/35 O0, 9/54 Os, 19/220 O3** ineffective hits in the 9 functions I ran; all O0/Os losses are in `caddq`/`reduce` functions, whose output differs only by a multiple of q. This is ported; whether the representative matters depends on the caller.
- `derive_all` compares only the first output (`qemu_inputs.py:535`), so `poly_decompose`/`polyveck_decompose` never test a0/v0.
- direct_leak is 7 rows; at O0 it reproduces 0 of 2.
- `MBC_PREFER_CALLER` (`qemu_inputs.py:351`) silently changes layouts through the environment and is not in the checkpoint key.

---

## Checked and clean
- **Blindness of code.** `find_faults.py`, `qemu_inputs.py`, `tests.py`, `emu.py` read only the ELF, IR, config, `function_inputs` and optional witness files. I grepped for the report, `results`, function names, hex PCs and opcodes: nothing but the scheme constants, distribution table, memory map and the name-prefix strip. The IR is used for signatures and buffer lengths, not fault sites. Relabelling changes debug lines only.
- The config's `output` keys are unused by `find_faults.py`; config secrets match the pinned QEMU-test config for the functions in common.
- **Tests and sweep** match QEMU-test's `early_stop_sweep_unicorn.py` and `common/ineffective.py` (read side by side): same grid, stop rule, clamping, skipped-sv rule.
- **Index alignment.** All 133 faults point at an IR instruction of the right kind (`a7.py`: 86 call/branch, 44 binop, 3 load/store, 0 mismatches). `data/fault_report_dilithium.csv` equals QEMU-test's copy.
- **Genuine port.** 30 of 33 reproduced O0 IR-yes rows have the identical witness; the 3 differences are noted (one stale-register difference in `poly_decompose`, two in `poly_uniform_eta`).
- **Non-XOF detections are robust to more seeds**: with seeds 0..15, all 76 O0 and 98 Os detected PCs in the 18 non-XOF functions I ran keep their ineffective/correction verdicts (2 Os direct_leak hits are lost).
- O0 and the server's O3/Os_clang reruns reproduce. No early-stop artefact found: once a sweep has a witness it is reported as is, and "no" verdicts use the full sweep.
- O3 "any" does beat a random-PC null in kappa; the PC-to-row mapping carries some information there.
- The submission's CSVs equal `targeted_unicorn/results/server/*` byte for byte.

## Not checked
- Full re-sweep of all builds (cost). Reg/stack/output-fill, secret-pos and modeq results are on subsets, as stated. Shared server load may affect wall-clock timeouts, not verdicts.
- The README's "138 cases byte-identical" claim, and the real `qemu_witness.json` files, which do not exist here.
- Whether Unicorn's skip is faithful in IT blocks, and whether the instruction-skip model matches real hardware.
- IT-block/literal-pool decoding of the linear disassembly beyond noting never-fired PCs.
- Kyber/MAYO, other Dilithium modes, and any real-device evidence.
