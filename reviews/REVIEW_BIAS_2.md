# Second adversarial review: bias, leakage, overfitting, overclaiming

Subject: the frozen snapshot (line numbers refer to it). The server copy hashes identically to the snapshot. All emulation ran on the server with the final code, unmodified, imported by the driver scripts next to this file (`ctl.py`, `rnd.py`, `callee.py`, `ntt.py`, `inv.py`, `stale.py`). The CSV work is in `tab.py`, `st.py`, `st2.py`, `bl.py`, `det.py`, `dom.py`, `hist.py`, `cmp.py`, `fillan.py` and `pos.py`. I formed my view, and designed and started the controls, before opening `reviews/`. The runs that finished later were not changed after reading it.

## Verdict

- **Blind: yes.** I found no path from the report, IR fault sites or earlier results into the fault search. There is no hardcoded PC, opcode, function or verdict.
- **Numbers: almost all correct.** I recomputed every figure in REPORT.md that has data behind it. Only small slips remain (72% vs 71%, 12.9% vs about 11.4-12.0%). A few figures have no data in the repository.
- **Headlines: mostly honest, with one wrong causal claim and some unsupported inference.** The report now puts the trivial baselines next to the agreement figures.
  - **Summary 5 ("Optimised builds leak directly": 0 / 15 / 3 / 236) is misleading.** The zero at O0 comes from what was swept and from the early stop. Four O0 callee instructions leak directly under their callers' inputs, and one swept O0 instruction leaks as soon as the secret is planted elsewhere or not at all. The 236 leaks at O3 and the 15 at Os are the same 8 groups (7 IR sites plus one unmapped group).
  - **"Same inputs" is false for at least `poly_ntt`.** It causes the single missing call skip, and I reproduced the IR witness exactly by removing a distribution label.
- **One new bug that changes results:** the skip decodes its instruction size from memory left by the previous instruction's last trial. My rerun of O0 got one wrong row this way (1 of 801). I found no published row that is wrong because of it, but 163 slow O0 instructions were not rerun.
- **Controls on the final code:** 44-61% of "correction" hits remain with no secret planted. At Os, 5 of the 15 direct leaks and one of the two reproduced value faults depend on registers being zero.

---

## Findings, by severity

### 1. (High) "0 direct leaks at O0" is an artefact of scope, and "236 at O3" is not 236 findings
REPORT.md:10 (summary 5) and the table at REPORT.md:21-26.

**(a) O0 code does leak.** Only a function's own symbol range is skipped (`find_faults.py:309`). At O0, `polyveck_add` calls `poly_add`, so the leaking `add` is only ever tested as `poly_add`, where the config names the *other* operand (`b`) as secret. Inlining at Os/O3 puts the same code under `polyveck_add`, where `u`, the operand that survives, is the secret. I ran the submission's own `_pc` on callee instructions under the caller's inputs (`callee.py`; 170 O0 and 111 Os_clang (caller, PC) pairs, 7 callers with callees):

| build | direct_leak "yes" | distinct PCs | which |
|---|---|---|---|
| O0 (published 0) | **7** | **4** | `poly_add 0x12ce add r1,r3` under `polyveck_add` and `polyvecl_add` (output holds secret `u`, 16 of 16 traces); `poly_sub 0x130a` under `polyveck_sub` (eph secret `u`); `decompose 0x4c36` and `0x4c5a` under `poly_decompose` and `polyveck_decompose` |
| Os_clang (published 3) | **12** | **7** | the same three callees |

So the instruction the report uses to say that O0 does *not* leak `b` leaks the secret when called from `polyveck_add`. `decompose` has no input sample, so it is never swept at O0 or in clang Os, but it is swept, inlined, at Os/O3.

**(b) One swept O0 instruction leaks, and the early stop hides it.** `poly_make_hint 0x1460 bl` (and `0x1920` in clang Os) leaves secret `a0` in `h`. The published detail says so: "output 'h' at [1, 2, 3] holds input 'a0' ... but the value never varied across the traces". Both other tests hit after two secret values (s=0, where nothing changes, and s=-95232), so the traces hold one planted value and direct_leak answers "no". It answers **yes** with the secret at position 4 or 512 (16 of 16 traces), with no secret planted, and with a redrawn background. QEMU-test has the same stop rule, so this is inherited, but the table's O0 zero rests on it. 2 (O0), 12 (Os), 1 (clang) and 38 (O3) published "no" verdicts carry the same "never varied" note. I did not resolve the others.

**(c) The counts are unrolling.** By DWARF site, the 15 leaks at Os and the 236 at O3 are the **same** set: `poly_add` lines 5, 7, 8; `poly_sub` 7, 8; `decompose` 6, 11; and one group of unmapped instructions in `poly_decompose`. So the counts do not show a trend with optimisation.

**(d) Not all are the secret.** 74 of 236 (O3) and 3 of 15 (Os) leak `eph_secret`, not `secret-buf`.

**(e) There is little to compare with.** Outside `signature_internal`, the IR study has direct_leak rows for `poly_add` only (7 rows). 13 of the 15 leaks at Os and 235 of the 236 at O3 are in functions with no IR direct_leak verdict.

**Suggested wording.** "direct_leak fires on 0 of the O0 instructions as swept. This sweep does not cover callee code under the caller's inputs. Under them, 4 O0 instructions in `poly_add`, `poly_sub` and `decompose` leak too, and a skipped call in `poly_make_hint` leaks when the planted value varies. Inlining brings such code into the sweep at Os and O3, and unrolling repeats it: 15 and 236 instructions are the same 7 IR sites and one unmapped group. About a third of the O3 hits (74) concern the ephemeral secret."

### 2. (High) State still leaks between instructions: `Skip` reads its size from dirty memory
`emu.py` `Skip.__init__`: `self.size = machine.disasm_one(pc)[1]`. `find_faults.py:113` builds the `Skip` **before** the first `h.run(..., restore=True)`. If the last trial of the previous instruction on that Machine (a runaway copy or store loop) overwrote code, a 4-byte instruction is decoded as 2 bytes, the skip lands in the middle of it, and the row is wrong.

- **Observed:** rerunning 801 O0 instructions, one row differs: `polyvecl_pointwise_poly_montgomery 0xe5a bl` (published: 1,024 trials, 0 crashes, ineffective yes; rerun: 256 of 256 crashes, `UC_ERR_INSN_INVALID at +0x2c`, exactly pc+2).
- **Mechanism confirmed** (`stale.py`, first secret value only): after 23 O0, 0 Os, 2 Os_clang and 1 O3 instructions (O3 without `ntt`/`invntt_tomont`), re-decoding the same function gives wrong sizes for 1 to 10 of its instructions.
- **Scope.** It depends on task order and worker assignment, so it is not deterministic. Reruns: Os 699/699, O3 2,413/2,413 and O0 800/801 identical. The published row above is the correct one. I have no evidence of a wrong published row, but the 163 O0 instructions with more than 20 s, and most O3 ones, were not rerun.
- `qemu_inputs.py` ("trials are independent of their order") and the state tests (`tests/test_state.py`) do not cover this, and REPORT.md's History says the state leak was fixed.

**Fix:** restore memory before constructing `Skip` (or take the size from `function_insns`, already computed in the parent), and add a test. Rerun at least the functions containing the 26 leaking instructions.

### 3. (High) "The same inputs" is false for `poly_ntt`, and the report does not say so
README.md:14 and 17-24; REPORT.md:3. `data/fault_report_dilithium.csv` gives for the skipped call in `poly_ntt`: `DETECTED at pos 916 (s1=12 ...)`. The submission's domain there is the 5 values of [-2,2] on 256 int32 words, so `s1=12` and position 916 are both impossible.
I removed the distribution from the layout (256 byte values, bytewise compare) and ran the submission's own code (`ntt.py`): **pos 916, s1=12, s2=0: the exact IR witness**, where the published code says "no". So the 30th IR-positive call skip is a set-up mismatch, not a skip that fails to carry over. With the byte set-up, 30 of 30.
It also flips `polyveck_ntt` and `polyvecl_ntt` to "yes" (same witness), where the IR report says "no". The IR rows were therefore made with **different settings for different functions**. The IR-level seconds (26.8 s for `poly_ntt`, 5-value scale, against about 420 s for a 256-value sweep of `poly_invntt_tomont`) also point to a different run than the report row.
**Fix:** state it, next to the 29/30.

### 4. (Medium) The O3 random baseline is an artefact, and the single P value pools opposite effects
REPORT.md:41-42; `correlate.py:269-289`.

- **O3 "P = 0.00 / 0.00".** A row passes if *any* mapped instruction is "yes". At O3 a value row maps to 59 instructions on average (up to 138), so random draws say "yes" nearly always: 48 false "yes" against 14. Drawing **one** random instruction per row gives 92.9 against 84 observed (P = 0.98), and always-"no" scores 90. The mapped rows do **not** beat chance at O3. Printed without comment, it reads as significant. With 2,000 draws it should also be "< 0.0005", not "0.00".
- **Pooling.** (600 draws; n = agreements of observed / null mean, theirs / one random instruction.)

| | O0 | Os | O3 |
|---|---|---|---|
| call skips (70 / 20 / 12 rows) | **66** vs 53.4 / 50.0, P < 0.002 | 17 vs 16.0 / 13.8, P 0.42 / 0.13 | 11 vs 11.0 / 10.8, P 1.0 / 0.90 |
| value faults (95 rows) | **69** vs 82.2 / 81.6, P 0.993 | 84 vs 82.0 / 81.3, P 0.46 / 0.37 | 73 vs 41.7 / 81.9, P 0.00 / 0.98 |
| (site, test) units (96 / 46 / 38; 2,000 draws) | **86** vs 71.5, P < 0.0005 | 37 vs 34.3, P 0.16 | 29 vs 20.5, P < 0.0005 (any-rule artefact again) |

Summary 3 ("at O0 and Os random instructions agree about as often") hides this. At O0 call skips beat chance clearly and value faults are *worse* than chance. At Os and O3 the reproduced call skips (11 and 6, all in `*uniform*`) are no better than random instructions of those functions (Os 10.8 reproduced on average when undecided instructions are left out, O3 6.0, against 11 and 6). So "11/12" and "6/7" reproduced call skips at Os/O3 carry **no evidence**. REPORT.md:60 says only where they are.
- **Comment/code mismatch.** The docstring says "random *executed* PCs" (`correlate.py:269`), but `by_fn` (line 270) includes instructions that never ran or crashed everywhere and counts them as "no", while the observed side drops them. Restricting to decided instructions raises the O0 null to 137.5 (above the observed 135), and the kappa P from 0.09 to 0.30. The direction is unfavourable to the submission, but the description is wrong.

**Suggested wording:** give the null by fault type, and say that at O3 the P value reflects the any-rule.

### 5. (Medium) The sample behind "value faults mostly do not carry over" is four sites in three functions
REPORT.md:7. The 10 IR-positive value rows are at 4 sites (`poly_add` 5 and 8, `poly_pointwise_montgomery` 10, `poly_sub` 8). Ignoring dependence, the Wilson 95% intervals are 4/10: 17-69%, and 2/10: 6-51%. The 95 rows come from 14 sites in 6 functions. Resampling functions (4,000 draws), agreement minus always-"no" is:

| | O0 | Os | O3 |
|---|---|---|---|
| all rows | +0.06 [-0.07, +0.28] | +0.07 [+0.01, +0.23] | -0.06 [-0.16, +0.15] |
| call skips | +0.37 [+0.23, +0.54] | +0.45 [+0.17, +0.80] | +0.50 [+0.17, +0.83] |
| value faults | **-0.17 [-0.29, -0.04]** | -0.01 [-0.04, 0.00] | -0.13 [-0.22, -0.02] |

The O0 direction is supported. "2 of 10" at Os and O3 is not: 1 of the 2 at Os disappears with non-zero registers (finding 7). REPORT.md:62 says that rows are not independent, but prints no effective sample size or interval.
Also, all agreement claims use different row sets (165, 115, 107) with 18 and 23 of the 40 swept IR-"yes" rows dropped at Os and O3. Use one denominator: **33 / 13 / 8 of 40 swept IR-"yes" rows** (83%, 33%, 20%). Counting unmapped as "no": agreement 137/167, 135/167, 121/167 against always-"no" 127/167. The report gives "of all 93", which mixes in 53 unswept rows.

### 6. (Medium) "Both act once per call" is contradicted by 8 of the 23 "IR no, machine yes" rows
README.md "Fault models differ". At O0, `invntt_tomont 0x2210 add ip,r3` (IR line 25) leaves `a[j+len]` in the destination: architecturally that *is* the IR model `opB` on the first iteration, and it must be ineffective at s=0. The skip says so (s=0 ineffective, correction alpha=16382). The IR report says "no" for `opB` and all the other models. The byte-wise set-up does not explain it (`inv.py`: still "yes"). So here either the IR fault did not act on the first iteration alone (the generator leaves loops alone that it cannot unroll, `binOp_load_store_Fault.cpp` `LabeledUnrollPass`), or the inputs differed. The report gives `poly_add` and `poly_sub` as examples (12 rows) and is silent on `invntt_tomont` (8). It does not affect the totals, but it is an unexplained conflict between the two tools that is not an "other-model" effect.

### 7. (Medium) Results depend on the harness: zero registers and stack, secret position 0, first execution
Final-code controls are in the table below. Points that matter:
- **Zero state.** With random registers and stack, 34 of 156 rerun O0 detections disappear (22%; none is new), and 21 of 188 at Os (3 new; 34 Os instructions lose a "yes" in at least one test). At Os, **5 of 15 direct leaks vanish** in both fills, and the `poly_add` line 5 `mem` row flips to "no" in both: one of the two value-fault rows reproduced at Os exists only because `ip` is 0 at entry. O0 report rows are unaffected (0 of 129 flip; Os 1 of 113). The unfaulted outputs are identical under every fill, so all of this is fault-side. REPORT.md:81 ("Registers start at zero") is disclosure without numbers, and does not mention the stack or the output buffers (the old review's fix, random fill, was not done).
- **Position.** Skip (first execution) and secret (coefficient 0) are matched. Outside `*uniform*`, at positions 4 and 512 the numbers of detected instructions are: O0 82 -> 62 / 64 (kept 53 / 51), Os 123 -> 64 / 67 (51 / 51), O3 334 -> 332 / 254 (287 / 239). The union of the three is 101 / 142 / 394 (1.2x / 1.15x / 1.2x). The report's "about three times as many O3 detections when several positions were combined" was 5 functions and 482 instructions (44 -> 134) and 4 nearby positions. Over half the O3 code the effect of two positions is 1.2x. It also does not say that at O0 and Os **fewer** instructions are found at another position.
- Positions hardly affect direct_leak (Os 15/15, O3 127/129), apart from the new O0 `poly_make_hint` leak of finding 1.

### 8. (Medium) The tests fire on what is not the secret
Disclosed at REPORT.md:79, but with numbers from older code. Re-run (table below):
- **No secret planted**: correction keeps 64/116 (55%) at O0, 54/106 (51%) at Os, 42/69 (61%) in clang Os, 313/712 (44%) at O3. The published 58% is the old O0 figure. Outside `*uniform*`: 23%, 30%, 32% and 56%. Ineffective cannot fire by construction, so the control says nothing about it.
- **Public coefficient swept instead of the secret** (7 functions with a public pointer input): ineffective 21 vs 22 (O0, 163 instructions), 49 vs 40 (Os, 156). Sweeping a public input triggers the ineffective test as often.
- **Background redrawn for each secret value, no secret planted**: ineffective still "yes" at 99 + 17 new of 135 (O0) and 105 + 23 new of 164 (Os). (Every input varies, the secret buffer included, so this only shows that the test asks for data dependence, not secret dependence.)
- **Not only the `*uniform*` functions.** With unrelated outputs drawn from the declared output distribution, ineffective fires 10/10 (and correction 9/10, 5/10, 10/10) for `poly_decompose`, `polyveck_decompose` and `poly_make_hint`. REPORT.md:79 names hint outputs but not the w1 outputs of `*decompose` (15 O0, 28 Os detections). `polyvecl_add` is compared byte by byte as well, which REPORT.md:29 reserves for `*uniform*`.
- In the report the uniform share is stated per build (79 of 163 at O0), but not that these functions hold only 24% of the instructions (230 of 964).

### 9. (Low) Scope the report does not state
- **Public data treated as secret.** `poly_uniform` (seed rho) and `polyveck_shiftl` (t1) supply 17 (O0), 22 (Os) and **287 (O3)** detections. README and REPORT do not say so.
- **Distinct sites.** Detections fall on 108 (O0), 93 (Os) and 127 (O3) IR sites. A report row exists at 45 of 163, 17 of 189 and 38 of 1,482 detected instructions (own function). "The sweep finds X" is therefore mostly outside what the IR study tried, which the report does not say.
- **Coverage figure.** "12.9%" was measured with `open` in the sweep and another denominator. For the 31 functions I get 2,230 of 19,528 bytes = **11.4%** at O0 (13.4% Os, 23.0% O3), or 12.0% on the old denominator. The report says it predates the exclusion of `open`, so it is disclosed, but it should be updated.
- `MBC_PREFER_CALLER` (`qemu_inputs.py:351`) still changes layouts through the environment and is not in the resume key (`find_faults.py:273-280`).
- **Wording error.** REPORT.md:34: a row is reproduced "if any instruction ... gets the same verdict". The code says "yes" if any is "yes", so an IR "no" row agrees only if **none** is "yes" (`correlate.py:133-147`).
- The "72%" of O0 time is of the per-instruction sum (38,864 s). It is 71.3% of the 39,310 s shown in the table.

---

## Controls on the final code

Seeds 0-3, 16 workers. Instructions over a time cap were left out, as stated. "Kept/new" are relative to the published verdict of the same instructions. No run was cut off by its deadline. `O0_fill*`, `O3_fill*` and `Os_fillptr` used a 10 s instead of 300 s trial timeout (no timeouts occurred).

**(a) Unrelated outputs to the detectors** (`rnd.py`; 31 functions, 10 repetitions, 3 kinds of unrelated output; hits of 10 as ineffective/correction; direct_leak 0 of 930 runs):

| unrelated output | fires |
|---|---|
| drawn from the declared output distribution ("eta", "gamma1" and "uniform" are unknown labels, so random bytes) | ineffective 10/10 for all 6 `*uniform*`, `poly_decompose`, `polyveck_decompose`, `poly_make_hint`; correction 0-2/10 for `*uniform*`, 9/10 `poly_decompose`, 5/10 `polyveck_decompose`, 10/10 `poly_make_hint`; nothing else |
| uniform random bytes | ineffective 10/10 for the 6 `*uniform*` only; correction 0-1/10 |
| another unfaulted run's real output | ineffective 10/10 in 24 of 31 functions (2-3/10 in `polyveck_ntt`/`polyvecl_ntt`, 0/10 in `poly_ntt`, `ntt`, `poly_make_hint`, `polyveck_add`, `polyveck_sub`); correction 10/10 in five of the six `*uniform*` (all but `poly_uniform`, 0/10), 2/10 in `polyvecl_add`. Not a fair null outside `*uniform*`: such a run shares the background with probability 1/4, so most coefficients are then identical |

So the report's sentence holds for ineffective. For correction it holds for the eta/gamma1 vectors (10/10 with real unrelated outputs) but not for `poly_uniform` (0-2/10).

**(b) No secret planted** (`nosecret`; correction only, see finding 8):

| | instructions run | correction kept | of which outside `*uniform*` |
|---|---|---|---|
| O0 | 884 of 964 (<= 40 s; includes all 116 published hits) | 64 of 116 (55%) | 12 of 52 |
| Os | 699 of 704 (<= 120 s) | 54 of 106 (51%) | 18 of 61 |
| Os_clang | 585 of 588 (<= 100 s) | 42 of 69 (61%) | 10 of 31 |
| O3 | 9,665 of 9,678 (<= 40 s) | 313 of 712 (44%) | 146 of 263 |

direct_leak keeps 15/15 (Os), 3/3 (clang), 234/236 (O3): it does not need a planted value, so this is expected (new: `poly_make_hint` at O0 and clang Os, finding 1). Public sweep and redrawn background: finding 8.

**(c) Initial registers and stack** (all registers plus the whole 4 MB stack region; `rand` = random 32-bit values, `ptr` = valid stack addresses; output buffers still zero). Detected = any test:

| | run | published | also detected | new | kept outside `*uniform*` |
|---|---|---|---|---|---|
| O0 rand | 801 of 964 (<= 20 s) | 156 | 122 | 0 | 60 of 82 |
| O0 ptr | 801 | 156 | 153 | 8 | 80 of 82 |
| Os rand | 699 of 704 | 188 | 167 | 3 | 113 of 123 |
| Os ptr | 699 | 188 | 179 | 3 | 114 of 123 |
| O3 ptr | 4,826 of 9,678 (every second) | 755 | 738 | 56 | 324 of 334 |

(Report rows: O0 0 of 129 flip, Os 1 of 113, O3 0 of 22 checked.)

**(d) Other secret positions**: finding 7 (`pos` in bytes). Instructions: O0 801 / 624, Os 699 / 517, O3 4,826 / 4,197 of every second one, for positions 4 / 512. The 6 seed-only functions are excluded at 512, since the seed is shorter than that.

**(e) Callee code under caller inputs**: finding 1. **(f) Byte-wise `poly_ntt`**: finding 3. **(g) Plain rerun** (base): O0 801 (800 identical), Os 699/699, O3 2,413/2,413 (every fourth); all verdicts, details and trial counts compared.

---

## REPORT.md, claim by claim

`ok` = recomputed from `results/` or the source and correct. `wrong`, `misleading` and `unsupported` are explained.

| Claim (line) | Status |
|---|---|
| 31 functions, every instruction skipped once (3) | ok, for the function's own symbol range. Callee code is not included (finding 1) |
| No IR-fault information used (3) | ok for the code. The function list comes from the IR study (stated in Limits) |
| Figures come from the code in this repository (3) | ok: 3,912 of 3,913 rerun instructions identical; the one difference is finding 2, and the published row is the right one |
| 1. 29 of 30 call skips at O0; "mostly checks the pipeline" (6) | ok, and stronger than stated: 29 of 29 "yes" at the `bl` itself; 30 of the 33 reproduced IR-"yes" O0 rows have the identical witness (position, secret values, alpha). But see `poly_ntt` (finding 3) for the 30th |
| 2. 4, 2, 2 of 10; below always-"no" in all three builds (7) | numbers ok (69, 84, 73 vs 85). Os is below by one row only. n is 4 sites; 1 of the 2 at Os is a zero-register artefact (findings 5, 7) |
| 3. 82 / 88 / 79% vs 76 / 81 / 84%; random instructions (8) | ok. Misleading by pooling (finding 4) |
| 4. `poly_add`: `a` at O0, secret `b` at Os (9) | ok |
| 5. direct_leak 0 / 15 / 3 / 236, "optimised builds leak directly" (10) | counts ok; the reading is **misleading** (finding 1) |
| Os size: 2,816 B / 2,812 B (16) | 2,812 ok (sum of the 38 symbols). 2,816 unsupported in the repository |
| What-the-skips-find table (20-26) | all 32 numbers ok |
| Counts not comparable (28) | ok |
| `*uniform*` functions inflate; compared byte by byte; tests fire on most unrelated outputs (29) | ok for ineffective. Overstated for correction in `poly_uniform` (0-2/10); incomplete, `polyvecl_add`, `*decompose` and hints too (finding 8) |
| direct_leak undecided: 186, 98, 129, 371, nearly all crash (30) | ok (all have detail "trace collection failed"; plus 4 timeouts at O3) |
| Where the leaks are (31) | ok |
| Definition of "reproduced" (34) | **wrong wording** (finding 9) |
| 150 rows of `signature_internal`, unmapped and unexecuted left out (34) | ok (317 = 150 + 167; 165 / 115 / 107 compared) |
| Agreement table (38-47), by-type table (53-58) | all entries ok (null means and P values within Monte-Carlo error). Interpretation at O3: finding 4. "By IR site" units are (site, test), 96 / 46 / 38, not sites (49 / 24 / 20) |
| At Os and O3 every reproduced call skip is in `*uniform*`; 15 of 29 elsewhere at O0 (60) | ok. Missing: no better than random there (finding 4) |
| "Other call sites are unmapped or inlined away" (60) | ok (18 and 23 IR-"yes" rows unmapped) |
| 23 IR "no" rows "yes" at O0; six in `poly_add`, six in `poly_sub` (61) | ok; 8 in `invntt_tomont`, the largest group, are not mentioned (finding 6) |
| Rows not independent (62) | ok, no effective sample size given |
| `poly_add` explanation (64) | ok: `add r1,r3` holds `a`; identical witness to IR `opA` (s=-4190208, alpha=4190209). Os `add r3,ip` leaks `b`, `opA` is "no" in both tests. "No single skip can match all four models" ok |
| 18,639 s / 83 faults; 39,310 etc. (69-70) | ok; 83 = distinct faults of the 31 functions in the report, all of which have rows. The timed run may not be the run the report rows come from (finding 3) |
| Elapsed 2,336 / 421 / 1,028 / 542 s (71) | **unsupported**: no file holds these. Plausible (longest O0 instruction 2,247 s; O3 16,726 / 32 = 523) |
| "Only indicative" (73) | ok |
| 30 of 964 over 300 s, 72%, mostly runaway or crashing returns (74) | 30 ok; 72% is of the per-instruction sum (71.3% of the table's total). All 30 crash on every secret value, so most of the O0 time yields no verdict, which should be said; 2,247 s ok |
| `poly_add` 46 s / 25 = 1.9 s vs 197 s (75) | ok. Compares one emulated skip with a rebuilt ELF per fault |
| Coverage: 31 of 39; 53 of 93 (78) | ok. 2 of 53 in the older run ok (older `summary.md`) |
| 12.9% (78) | old: **11.4%** (or 12.0% on the old denominator) for the final set; disclosed as pre-`open` |
| Tests fire by chance: 10 of 10; 58% (79) | matches the old review (68/118). On the final code 55% at O0, and 51 / 61 / 44% elsewhere. "Not re-run" is ok |
| "About three times as many O3 detections" (80) | true only of that subset; 1.2x on half of O3, and fewer at O0/Os (finding 7) |
| Registers start at zero (81) | ok as disclosure; effect is large (finding 7). Stack and output buffers not mentioned |
| Timeouts wall-clock; 4 at O3, all `pop` in `*uniform*` (82) | ok |
| Not run: main(), signature, counters (83) | ok |
| History: 3/3, 19/0, 8/1, 47/4; 137/167 -> 135/165 (86) | ok, recomputed against the old results for all four builds. "State leaked... fixed" is **incomplete** (finding 2). Os and O3 agreement also changed (97 -> 101, 82 -> 84), not mentioned |

**What a sceptical reader would misunderstand:** (i) that optimisation introduces leaks; (ii) that P = 0.00 at O3 supports the mapping; (iii) that the Os/O3 call-skip rates are evidence of anything; (iv) that the one missed call skip is a skip effect; (v) that 2 of 10 is a measured rate rather than one or two sites.

---

## Status of the earlier findings (REVIEW_BIAS.md)

| # | Finding | Status |
|---|---|---|
| 1 | Agreement does not beat baselines | **Fixed in reporting** (`cmd_stats`, summaries 1-3). The O3 null and pooling remain (finding 4) |
| 2 | Rows dependent; dropped rows; signature result | **Partly fixed**: site row, "of all 93" and 2/53 are in. Intervals and the 40-row denominator are missing; "by IR site" units are (site, test) |
| 3 | Tests fire by chance in hash/sampling functions | **Disclosed only**: no control column, so the bias remains. Not only `*uniform*` (finding 8) |
| 4 | Correction without a secret | **Disclosed only**. Reproduced (44-61%). `open` **fixed** (excluded; no report rows there, so this does not flatter agreement). `poly_make_hint`'s always-zero `a1` (calibrated to 0) is still there, not mentioned in the report |
| 5 | State and registers; zero memory | **Partly fixed**: registers, flags and memory restored before each run, and the reruns match. Zero fill not varied, and a new state leak through `Skip.size` (finding 2) |
| 6 | Function list and coverage; public data as secret; config wording | **Partly fixed**: coverage stated, README's config wording corrected, and "open" renaming is disclosed (README). Public data is **still open** |
| 7 | Position 0 and first execution | **Disclosed only**; the 3x figure is a subset result. The README caveat on first execution was **corrected** (but see finding 6) |
| 8 | Any-rule; foreign copies; distinct sites | **Partly fixed**: own-function PCs only. Any-rule unchanged; distinct sites not reported |
| 9 | Denominators, machine-only | **Fixed** (crash and never-executed columns; undecided not counted as "no"). Low: 72% and the row-coverage statement |
| 10 | Not blind in development; `poly_ntt` not the same experiment; resume key | Resume key **fixed** (content hash); golden timing **fixed** (no zero-cost functions in the O0 timing). `poly_ntt` **still open** (finding 3). No held-out scheme, parameter set or build: **still open**, not stated |
| 11 | Other (raw vs mod-q ineffective, first output only, 7 direct_leak rows, `MBC_PREFER_CALLER`) | direct_leak row count **disclosed**; `MBC_PREFER_CALLER`, the first-output-only compare and mod-q ineffective **still open** |

Other fixes verified: IT-block skips (13 unit tests pass; 10 IT members at Os, and none in the other builds, write PC; the 5 I looked at are function returns, where the NOP that stays in place has no later execution to affect), direct_leak fallback (runs the unfaulted side with the skip disarmed), memory restore.

---

## Checked and clean

- `find_faults.py`, `emu.py`, `qemu_inputs.py`, `detectors.py` and `ir.py` read only the ELF, IR, config, `function_inputs` and optional witness files. No function, PC, opcode or verdict is special-cased (grepped; "uniform" occurs only in `cmd_stats` as a row set). Relabelled IR carries no fault information. Task order does not depend on the report.
- Config: 33 entries of the pinned QEMU-test config unchanged (secrets and ephemeral buffers), `open` renamed, 6 added (all without samples); all 33 function_inputs files equal Dilithium-LLVM's.
- `open`: the exclusion removes 79 instructions (1,043 -> 964 at O0) and no IR report row.
- Early stopping cannot change a verdict (verdicts are latched; the direct-leak set is the same rule as QEMU-test's).
- Witness check: 30 of 33 (O0), 10 of 13 (Os) and 7 of 8 (O3) reproduced IR-"yes" rows have the identical witness, including the concrete alpha of `poly_add`. This also supports the rebuilt argument layouts, which the README calls unverified (except `poly_ntt` above; the differences are `poly_uniform_eta` `rej_eta` line 4 and `poly_decompose` line 7).
- All 2x2 tables, kappa, precision, recall, always-"no" and the Monte-Carlo means match my independent code; rows sharing a mapped PC set get the same draw.
- Undecided, not executed and not swept rows are not counted as agreement.
- The unfaulted outputs are identical under all register and stack fills.

## Not checked

- Full re-sweeps: O0 instructions over 20 s (163, 72% of the cost), most of O3, and Os_clang except the no-secret run. `fill:rand` on O3 and Os_clang was not run; `O3_base` covered every fourth, `O3_pos*`/`O3_fillptr` every second instruction.
- Elapsed wall-clock times and the 2,816 B figure (no data in the repository; I did not read outside my allowed server directory or the author's private logs).
- The real witness files; the "138 cases byte-identical" claim; hardware; whether the IR report rows and the IR timing come from one run (finding 3 suggests not).
- Why the IR study says "no" for `invntt_tomont` line 25 (finding 6): I could not run the IR generator.
- Full effect of finding 2 on the slow O0 instructions.
