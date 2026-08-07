# De-vendor `nrf_adapter_source_multiceiver/Drivers/`

## Context

This repo is a fork of someone else's project (top-level Unlicense) that has accumulated
eight third-party trees committed directly into
`nrf_adapter_source_multiceiver/Drivers/`. Their licenses do not agree with each other or
with the top-level one: **QNEthernet is AGPL-3.0-or-later** (vendored deliberately,
eyes-open, per `plans/nrf_adapter_teensy_ethernet_link_plan.md` decision 1), PJRC's SPI
library is GPL-2.0-or-LGPL-2.1, the PJRC cores carry an MIT variant with a non-standard
device-list clause, and CMSIS/TinyUSB are Apache-2.0/MIT. The Ethernet plan named
de-vendoring and a `THIRD_PARTY.md` as the mitigation and deferred both. This is that work.

The trigger is the intent to contribute changes back upstream. Today a would-be contributor
cannot tell where the fork's own code ends: **583 of the 666 tracked files under
`nrf_adapter_source_multiceiver/` are vendored** — the firmware actually maintained here is
83 files and ~335 KB, 3% of the directory's bytes.

De-vendoring here means exactly what was asked: git-ignore the vendored directories, drop
their contents from the repo (files stay on disk), and add a script that downloads each one
individually at a locked version, so someone building only for an STM32F030 never downloads
QNEthernet.

**Scope is `nrf_adapter_source_multiceiver/Drivers/` only** (confirmed). Explicitly out of
scope, and the licensing story stays incomplete because of it: `nrf_adapter_source/`
(the legacy Keil project's HAL/CMSIS/Modules — four of its libs carry no version marker at
all), `gui_source/qframelesswindow/` (a genuine **fork** of a GPL-3.0 project, with an added
`FullscreenButton` and five `set_*_btn_enabled` methods that do not exist upstream — it
cannot be re-downloaded), `gui_source/richuru.py`, and `.upx/`.

## Verified upstream pins

Every pin below was downloaded and diffed against the working tree during planning. These
are not inferences.

| Destination | Upstream | Pin | Diff vs. current tree |
|---|---|---|---|
| `Drivers/CMSIS/Include` + `LICENSE.txt` | `STMicroelectronics/cmsis-core` | tag `v5.4.0` | **0 files differ** |
| `Drivers/CMSIS/Device/ST/STM32F0xx/Include` | `STMicroelectronics/cmsis-device-f0` | tag `v2.3.7` | 3 kept headers identical |
| `Drivers/CMSIS/Device/ST/STM32F1xx/Include` | `STMicroelectronics/cmsis-device-f1` | tag `v4.3.5` | 3 kept headers identical |
| `Drivers/teensy3` | `PaulStoffregen/cores` | commit `7f107ee0a309f3813ed13f0d8f615497eca2ee49` | **0 files differ**, no exclusions |
| `Drivers/teensy4` | `PaulStoffregen/cores` | same commit | identical except `Blink.cc` (excluded) and the `imxrt1062_t41.ld` patch |
| `Drivers/teensy_libs/SPI` | `PaulStoffregen/SPI` | commit `7c83d0726746b652af37319e70cd3932c253ecae` | identical, `examples/` excluded |
| `Drivers/teensy_libs/QNEthernet` | `ssilverman/QNEthernet` | tag `v0.36.0` | `src/` (207 files) + 6 root files identical |
| `Drivers/tinyusb` | `hathach/tinyusb` | tag `0.21.0` | all 26 kept files identical |

**Correct the provenance claim while doing this.** `README.md` says the PJRC cores are
"Teensyduino 1.59, copied verbatim". They are not — they byte-match `PaulStoffregen/cores`
**master at `7f107ee0`, post-1.62**. Teensyduino 1.59's actual cores differ in 9 files
(teensy3) and 49 files (teensy4). The `-DTEENSYDUINO=159` in both target Makefiles is
**not** stale and **must not change**: upstream's own standalone `Makefile` at that exact
commit declares 159, the only gates anywhere in either core are two `#if TEENSYDUINO >= 159`
in `IntervalTimer.h`, and a scratch A/B build confirmed the Teensy 4.x flash image is
byte-identical at 159 vs 162. Fix the comment and the README; leave the number alone.

## Deliberate deltas from the current tree

Three places the fetched tree will not be a bit-for-bit copy of what is checked in today.
All three are improvements and must be recorded in the manifest's `note` fields.

1. **CMSIS device headers: fetch the whole upstream `Include/`** (18 files for F0, 16 for
   F1) rather than reproducing today's hand-pruned 3-file subset. The extra headers are
   include-only and cost nothing; pruning would mean carrying a per-file allowlist forever,
   and it is what makes retargeting to another STM32F0/F1 part a source edit instead of a
   vendor-tree edit.
2. **CMSIS device license files.** Today's `LICENSE.txt` is ST's Cube "see Package_license"
   pointer stub. Upstream ships the full Apache-2.0 text as `LICENSE.md`. Fetch upstream's.
3. **`Drivers/teensy_libs/SPI` comes from `PaulStoffregen/SPI`, not the Teensyduino
   installer.** Verified byte-identical across all five files, and it turns a 26 MB
   `.tar.zst` download into ~100 KB.

## The one local patch

`Drivers/teensy4/imxrt1062_t41.ld` line 51 is patched and **must survive re-download**:

```
-		*(.ARM.exidx* .ARM.extab.text* .gnu.linkonce.armexidx.*)
+		*(.ARM.exidx* .ARM.extab* .gnu.linkonce.armexidx.*)
```

Without it, `BOARD=TEENSY41 ETH=1` fails to link with `relocation truncated to fit:
R_ARM_PREL31` — libgcc's unwind objects carry a plain `.ARM.extab` that falls through to
orphan placement in ITCM, past the 31-bit encoding's range. Root-caused in
`plans/nrf_adapter_teensy_ethernet_link_plan.md` Phase 0. It is a no-op for every other
build.

Express it in the manifest as an explicit `{file, find, replace, why}` rule rather than a
`.patch` file: it is one line, it needs no external `patch` binary, and the script can
assert `find` occurs exactly once and fail loudly if a future pin bump moves it.

## Design

### `tools/vendor.json` — the manifest

One entry per tree. Follows no existing repo convention because none exists; keep it flat
and readable. Per tree: `name`, `description`, `license` (SPDX), `homepage`, `pin`
(tag or commit SHA, plus which), `archive_url`, one or more `{from, to}` extract mappings,
`exclude` globs, optional `patch` rules, `note`, and `tree_sha256`.

`tree_sha256` is the integrity check, and it is deliberately **not** a checksum of the
downloaded archive: GitHub's auto-generated tarballs are not byte-stable over time
(the January 2023 recompression incident changed every published checksum). Instead hash the
*result* — sha256 over `sorted(relpath + "\0" + sha256(content))` of the final on-disk tree,
computed after extraction, pruning and patching. That verifies the content that matters,
survives archive recompression, and catches a silently-dropped patch, which an archive
checksum would not.

### `tools/fetch_vendor.py` — the script

Python, matching the repo's unambiguous convention (`tools/noack_report.py`,
`net_provision.py`, `host_link_test.py`): no shebang, not executable, long module docstring
before imports ending in a `Usage:` block, `argparse.ArgumentParser(description=__doc__,
formatter_class=RawDescriptionHelpFormatter)`, long-form flags only, parser at module top
level. **Standard library only** (`urllib.request`, `tarfile`, `hashlib`, `json`,
`argparse`, `shutil`) — this script has to run before anything else exists, so it must work
under a bare `python3` as well as `venv/bin/python`.

```
python3 tools/fetch_vendor.py                          # everything (zero-friction default)
python3 tools/fetch_vendor.py --target teensy4x-eth    # just what that build needs
python3 tools/fetch_vendor.py --tree qnethernet        # one tree by name
python3 tools/fetch_vendor.py --list                   # trees, targets, pins, licenses, sizes
python3 tools/fetch_vendor.py --check                  # verify what's on disk, download nothing
python3 tools/fetch_vendor.py --force                  # re-fetch trees already present
```

Behaviour:

- Already-present trees are skipped after a `tree_sha256` check, so re-running is cheap and
  idempotent. A present-but-wrong-hash tree is reported, not silently overwritten.
- **Atomic**: download and extract to a temp dir, prune, patch, hash, verify, *then* swap
  into place. A failed or interrupted fetch never leaves a half-populated `Drivers/` subtree
  for the build to trip over.
- `--target` groups map to real build configurations, so nobody has to know which tree a
  board needs: `stm32f030` → cmsis-core + cmsis-f0; `stm32f103` → cmsis-core + cmsis-f1 +
  tinyusb; `stm32f103-usart1` → same minus tinyusb; `teensy3x` → cores-teensy3 + spi;
  `teensy4x` → cores-teensy4 + spi; `teensy4x-eth` → those plus qnethernet; `esp32` →
  nothing, with a one-line pointer to the `IDF_PATH` requirement so the answer is not silence.
- Failures name the tree and the URL. Non-zero exit on any failure.

`cores-teensy3` and `cores-teensy4` share one archive and one pin; fetch it once per
invocation and extract both subpaths.

### Makefile guards

No target currently checks that its trees exist, and the failure modes are poor — the Teensy
Makefiles use `$(wildcard …)`, which silently yields nothing, so `OBJS` quietly loses ~150
objects and the first error is an unrelated `fatal error: Arduino.h: No such file or
directory`. Add one existence guard per target, checking a specific file (not the directory,
which an interrupted fetch could leave empty), modelled on the existing
`targets/esp32/Makefile:167-169` `IDF_PATH` guard and matching its tone:

- `targets/teensy4x/Makefile` — check `$(FW_DIR)/core_pins.h` and `$(SPI_DIR)/SPI.h`; under
  `ifeq ($(ETH),1)` also `$(QNE_DIR)/QNEthernet.h`
- `targets/teensy3x/Makefile` — `$(FW_DIR)/core_pins.h`, `$(SPI_DIR)/SPI.h`
- `targets/stm32f103/Makefile` — `$(CMSIS_INC)/core_cm3.h`, `$(DEVICE_INC)/stm32f1xx.h`;
  under the CDC branch also `$(TU_DIR)/tusb.h`
- `targets/stm32f030/Makefile` — `$(CMSIS_INC)/core_cm0.h`, `$(DEVICE_INC)/stm32f0xx.h`

Each error names the exact `--target` invocation that fixes it.

### `.gitignore`

Anchored paths, because `Drivers/` exists in two places and `nrf_adapter_source/Drivers/`
stays tracked:

```
/nrf_adapter_source_multiceiver/Drivers/CMSIS/
/nrf_adapter_source_multiceiver/Drivers/teensy3/
/nrf_adapter_source_multiceiver/Drivers/teensy4/
/nrf_adapter_source_multiceiver/Drivers/teensy_libs/
/nrf_adapter_source_multiceiver/Drivers/tinyusb/
```

Removal is `git rm -r --cached` — files stay on disk exactly as asked.

### Git history

This is a personal fork: no one else has forked this copy, nothing from it has been pushed
to the original upstream project, and there is exactly one person using this version today.
Every commit that introduced a vendored tree was made **after** the fork — confirmed by
walking history, not assumed:

```
$ git log --oneline -- nrf_adapter_source_multiceiver/Drivers
05c11a6 add control over ethernet for teensy 4.1
9acca5c add f103 usb cdc port to nrf_adapter_source_multiceiver
89aea3d add stm32f103 (blue pill), teensy 3.5, 3.6 and 4.0 support
da0adfb add teensy 4.1 support
7facf79 add multi device support
```

None of those five are reachable from `main` or `multi-platform-wip` — the vendored trees
only exist on the `GetWorking` line. That means there is no reason to leave AGPL/GPL blobs
sitting in history "because rewriting is unsafe": nothing outside this machine depends on
these commit hashes. **This phase rewrites history to remove the five vendored trees from
every commit that carries them, not just from the tip** — since they're gone from history
entirely, there is nothing left for `THIRD_PARTY.md` to disclose about it.

**Refs affected**, checked directly rather than assumed:

| Ref | Vendor commits present | Action |
|---|---|---|
| `GetWorking` (local) | all 5 | rewrite |
| `origin/GetWorking` (gitlab remote) | 1 of 5 (`7facf79`) — the other 4 are local-only, unpushed | force-push rewritten history |
| `pre-rebase-backup-9a51a05` | 3 of 5 (`9a51a05`, `da0adfb`, `7facf79`) | stale backup branch from an earlier rebase, fully superseded by `GetWorking`'s current history and otherwise unreferenced — delete it rather than rewrite it |
| `main`, `multi-platform-wip`, `lxc-git/main` | none | untouched; confirm they stay that way after the rewrite |

**Mechanics — use `git filter-repo`, not `git filter-branch`** (deprecated upstream, far
slower, and riskier against a repo already at 73 MB). It's a single-file Python script, not
currently present in this environment; install it with `venv/bin/python`, per
`[[use-venv-python]]`:
```
venv/bin/python -m pip install git-filter-repo
```

A full copy of the repo directory already exists outside this checkout, so no extra backup
step is needed before rewriting. The rewrite still happens in a scratch clone rather than
this working copy in place, purely so a bad run costs a `rm -rf` of the scratch clone and
nothing else.

**This is the corrected procedure, updated after Phase 4 actually ran on 2026-08-07 — the
first two attempts below were wrong and are recorded as failure modes, not options:**

0. **Drop any local stash first.** `git clone --mirror` copies `refs/stash` into the scratch
   clone along with everything else, and `git-filter-repo` refuses to run against a clone
   that has one ("does not look like a fresh clone"). Check `git stash list` in the real
   working copy and drop or pop it before cloning — the scratch clone is disposable, but the
   stash itself is real, uncommitted work, so this is the user's call, not something to drop
   unasked.
1. **Rewrite in a scratch clone, scoped to exactly the commits that need it.**
   ```
   git clone --no-local --mirror . ../devendor-rewrite.git
   cd ../devendor-rewrite.git
   venv/bin/python -m git_filter_repo \
     --refs main..GetWorking \
     --path nrf_adapter_source_multiceiver/Drivers/CMSIS \
     --path nrf_adapter_source_multiceiver/Drivers/teensy3 \
     --path nrf_adapter_source_multiceiver/Drivers/teensy4 \
     --path nrf_adapter_source_multiceiver/Drivers/teensy_libs \
     --path nrf_adapter_source_multiceiver/Drivers/tinyusb \
     --invert-paths
   ```
   **The `--refs main..GetWorking` scope is not optional — two weaker forms were tried and
   both corrupted history that should have stayed untouched:**
   - **No `--refs` at all** processes every ref in the mirror (`GetWorking`, `main`,
     `multi-platform-wip`, `pre-rebase-backup-9a51a05`). `git-filter-repo` unconditionally
     strips `gpgsig` from every commit it walks — a signature over a pre-rewrite tree/parent
     would be invalid anyway — and stripping a field changes that commit's hash, cascading to
     every descendant. `main` carries GPG-signed commits from the original upstream fork, so
     this silently gave `main` and `multi-platform-wip` **entirely new hashes** even though
     neither one ever touched `Drivers/`.
   - **`--refs GetWorking`** (no range) correctly leaves the `main` and `multi-platform-wip`
     *refs* alone, but `GetWorking` branched off at `main`'s current tip, so all 97 of
     `main`'s commits are still ancestors of `GetWorking` and still get walked and re-hashed
     as part of rewriting `GetWorking`'s line. Symptom: `git merge-base main GetWorking` moved
     from `main`'s real tip to a commit ~14 spots earlier — real, unrelated shared history
     silently duplicated under new hashes, which would show up as spurious conflict surface on
     any future merge of `GetWorking` back into `main`.
   - **`--refs main..GetWorking`** limits the walk to exactly the 28 commits unique to
     `GetWorking` (confirmed: `git rev-list --count main..GetWorking` = 28, and
     `git-filter-repo` itself reports `Parsed 28 commits`). `main`'s tip becomes a fixed,
     unrewritten boundary/parent that `GetWorking`'s first rewritten commit points at, so
     `main` stays byte-identical *and* `GetWorking` keeps real, hash-identical shared ancestry
     with `main` up to the fork point.
   Run this after Phase 3 has landed the `.gitignore`/`git rm --cached` commit, so the
   rewritten history's tip matches what Phase 3 already produced and the only effect of the
   rewrite is on older commits.
2. **Verify before anything is pushed** — all of the following, not just the first one:
   - `git log --oneline refs/heads/GetWorking -- nrf_adapter_source_multiceiver/Drivers`
     in the rewritten clone returns nothing.
   - `main`, `multi-platform-wip`, and `pre-rebase-backup-9a51a05` have **identical**
     `git rev-parse` output in the rewritten clone vs. the real repo (proves the `--refs`
     scope actually held).
   - `git merge-base main GetWorking` in the rewritten clone still equals `main`'s real tip
     (proves shared ancestry wasn't duplicated).
   - `git count-objects -vH` / `du -sh` shows the repository shrank.
   - Spot-check a couple of the five original commits: find the same subject lines **with
     `git log --oneline refs/heads/GetWorking --grep=... -F`, not `--grep` against `--all`** —
     the mirror clone still carries a leftover `refs/remotes/origin/GetWorking` pointing at
     the old, unrewritten tip (harmless, never pushed, but it makes `--all` return both the
     old and new hash for the same commit message and will break a naive diffstat comparison).
     `git show --stat` each rewritten commit against `':!.../Drivers'` and diff it against the
     same command run on the original hash in the real repo — confirm the non-vendor part of
     the diff is byte-identical.
3. **Push is the user's step, not part of this implementation work** (standing rule —
   `[[user-does-commits]]` extends to pushes, resets, branch deletion, and `gc` — see
   `[[user-does-commits]]`). Once the rewrite is verified, hand off the exact commands rather
   than running them. Two mirror-clone artifacts need clearing first, or the push fails before
   it even reaches the branch-protection check:
   - `git clone --mirror` points `origin`'s URL at the local source path (`.`), not the real
     remote — `git remote set-url origin <gitlab-url>` first (`git remote add` will error with
     "remote origin already exists").
   - `git clone --mirror` also sets `remote.origin.mirror = true` in config, which forces
     *any* push through that remote to behave as a full mirror push and rejects a scoped
     refspec with `fatal: --mirror can't be combined with refspecs` —
     `git config --unset remote.origin.mirror` first.
   - Push **only the one branch that changed, by explicit refspec** — not `--all` and not
     `--tags` (confirm first with `git tag`; there were none to worry about). `--all` also
     tries to force-push `main`, `multi-platform-wip`, and `pre-rebase-backup-9a51a05`
     unchanged, and GitLab's protected-branch pre-receive hook rejects every one of those
     (`main` included) even though nothing about their content would have changed — this was
     tried and rejected in practice, which is how the `--refs` scoping bug above was caught in
     the first place.
   ```
   git remote set-url origin git@vm-gitlab.debugplus.net:main-group/mdp-p906-controller.git
   git config --unset remote.origin.mirror
   git push --force origin refs/heads/GetWorking:refs/heads/GetWorking
   ```
   `GetWorking` itself is not a protected branch on this project, so this push needs no
   GitLab settings change (only `main` was ever rejected, during the `--all` mistake above).
   `lxc-git` never received `GetWorking` and needs nothing here.
   `pre-rebase-backup-9a51a05` is local-only, so there's nothing to delete on a remote.
4. **Re-point the working copy**, after the push above is confirmed done. Rewritten commits
   have new hashes, so the existing working directory's branches point at orphaned history
   until this runs: `git fetch origin`, then `git reset --hard origin/GetWorking`. Delete the
   local `pre-rebase-backup-9a51a05` branch. Then reclaim the old objects locally — they linger
   as loose objects/reflog entries until forced out — with
   `git reflog expire --expire=now --all && git gc --prune=now --aggressive`. All four of these
   are the user's commands to run, same standing rule as the push.
5. **No one else to notify.** Confirmed above: no forks, nothing pushed upstream, single
   user. The standing cost of a history rewrite (every other clone must be discarded and
   redone) is real but pays to zero today; note it here so it isn't forgotten if that ever
   stops being true.

## Phases

### Phase 1 — Manifest and script

Write `tools/vendor.json` and `tools/fetch_vendor.py`. Nothing is deleted from git yet.

### Phase 2 — Prove it reproduces the current tree (the gate)

Before anything is removed, `--check` must pass against the working tree exactly as it
stands. Then, per tree: move the current copy aside, fetch, and `diff -r` against the moved
copy. Expect zero differences except the three documented deltas (whole CMSIS `Include/`,
upstream `LICENSE.md`, and the excluded `Blink.cc`/`examples/` which are absent from both).
The `imxrt1062_t41.ld` patch must be present in the fetched copy. **This phase is the gate —
nothing in Phase 3 happens until it passes for all eight trees.**

Once it passes, commit `tools/vendor.json` and `tools/fetch_vendor.py` on their own, before
Phase 3 touches anything. This is a non-destructive, independently-verified checkpoint —
keeping it as its own commit means Phase 3's `git rm --cached` and Phase 4's history rewrite
can each be backed out without taking Phase 1's work with them, and it matches Phase 4
already anchoring itself to "Phase 3's commit" as a discrete landing point.

### Phase 3 — Remove from git

`.gitignore` entries, then `git rm -r --cached` the five paths. Verify `git status` is clean
afterwards and that all eight trees are still on disk.

### Phase 4 — Purge the vendored trees from git history — DONE 2026-08-07

Everything through Phase 3 only stops *new* commits from re-adding the vendored blobs; the
five commits that originally added them (`7facf79`, `89aea3d`, `da0adfb`, `9acca5c`,
`05c11a6`) still carried them in full. See **### Git history** above for the verified fact
pattern (personal fork, single user, all five commits post-fork) and the exact,
twice-corrected `git filter-repo` procedure (`--refs main..GetWorking`, not a bare or
under-scoped `--refs`). Run in a scratch clone right after Phase 3's `.gitignore`/
`git rm --cached` commit landed — at that point `Drivers/` was already untracked at the tip,
so the rewrite's only effect was on those five older commits.

Result: `GetWorking` rewound and rebuilt to `5a63866` (same tip tree as Phase 3's `89ef31c`,
just rewritten ancestry), force-pushed to `origin/GetWorking`, working copy re-pointed and
`gc`'d (`.git` 73M → 55M). The five original commits survive under new hashes
(`7facf79→202bdd2`, `89aea3d→c433c5c`, `da0adfb→d522f90`, `9acca5c→256e23b`,
`05c11a6→6b2ac28`) with their non-vendor diff verified byte-identical to the originals.
`main`, `multi-platform-wip`, and `lxc-git/main` verified untouched (identical hashes, and
`git merge-base main GetWorking` still resolves to `main`'s real tip). `pre-rebase-backup-
9a51a05` deleted locally (it was local-only — confirmed via `git ls-remote` against both
`origin` and `lxc-git` before deleting, it was on neither).

### Phase 5 — Makefile guards — DONE 2026-08-07

Add the guards. Verify each fires with the intended message by pointing the tree variable at
a nonexistent path, not by deleting anything.

Result: one guard block per tree per target (8 total, matching the "Makefile guards" design
section above), each checking a specific header file rather than the directory. All 8 fire
with the correct `--target` invocation named, verified by overriding each target's tree
variable (`FW_DIR`, `SPI_DIR`, `QNE_DIR`, `CMSIS_INC`, `DEVICE_INC`, `TU_DIR`) to
`/nonexistent` on the command line — nothing was deleted from disk. A normal build (no
override) still succeeds on `teensy4x` and `stm32f030` with the guards in place; `make clean`
run after on every target left `git status` showing only the four Makefile edits.

### Phase 6 — Documentation — DONE 2026-08-07

- **`nrf_adapter_source_multiceiver/README.md`** — the largest edit. The `Drivers/` layout
  block (~lines 112-132) becomes the pin table, and gains **QNEthernet, which the README
  does not mention even once today** despite being the largest tree and the only AGPL one.
  `## Building` (~693-712) gains the fetch step ahead of `make`, and its "no package manager,
  no board manifest, no downloaded toolchain" sentence needs rewording. The
  `### Ethernet host link (ETH=1)` section (~311-349) gains the QNEthernet prerequisite.
  `## Refreshing a vendored tree` (~796-828) is rewritten: refreshing is now "bump the pin in
  `tools/vendor.json`, re-fetch, re-record `tree_sha256`". Two things there must survive the
  rewrite — the Teensy 3.x `WDOG_TOVALL` re-calibration warning, and TinyUSB's device-side
  subset / `-std=gnu11` note. One thing must be **deleted**: the instruction to "treat
  anything resembling a local modification as a finding", which followed literally would
  revert the `.ARM.extab*` patch.
- **`readme_EN.md`** §Modification Method (~74-117) — add the fetch line before `make`.
  Never touch `readme.md`.
- **`THIRD_PARTY.md`** (new, repo root) — the license inventory the Ethernet plan deferred:
  each tree, upstream, pin, SPDX license, and what it is linked into. States plainly that
  AGPL-3.0 §13 applies to `ETH=1` images, that GPL-2.0-or-LGPL-2.1 SPI is in every Teensy
  image, and that the top-level Unlicense never described `Drivers/`. Says nothing about git
  history — by the time Phase 6 lands there's nothing left in it to disclose. Notes that
  `gui_source/qframelesswindow/` (GPL-3.0, forked) is still vendored.
- **`plans/nrf_adapter_new_targets_port_plan.md:24`** — its standing rule "vendored trees
  under `Drivers/` stay byte-for-byte upstream" is now superseded; add a pointer.
- **`plans/nrf_adapter_teensy_ethernet_link_plan.md`** decision 1 — note that its deferred
  `THIRD_PARTY.md` and de-vendoring are now done.

Per `[[protocol-doc-vs-research-doc]]`: READMEs and `THIRD_PARTY.md` state current state
only; the "cores were never actually 1.59" finding and the 159-vs-162 A/B belong in this
plan document, and neither README links here.

Result: all six items above landed. `nrf_adapter_source_multiceiver/README.md`'s `Drivers/`
layout block is now the pin table (with QNEthernet), the provenance correction sits directly
below it, `## Building` gains the fetch step and reworded sentence, `### Ethernet host link
(ETH=1)` gains the QNEthernet fetch/prerequisite note, and `## Refreshing a vendored tree` is
rewritten around `tools/vendor.json` + `--update-hashes` with the WDOG_TOVALL and TinyUSB
notes carried over verbatim and the "treat any local modification as a finding" line deleted.
`readme_EN.md`'s Modification Method gained the fetch line ahead of `make`; `readme.md`
untouched. `THIRD_PARTY.md` written at the repo root, including the out-of-scope
`nrf_adapter_source/` and `gui_source/qframelesswindow/` notes (`zhiyiYo/PyQt-Frameless-Window`,
GPL-3.0, confirmed by web search rather than assumed) and no git-history discussion. Both plan
documents got their pointers.

## Critical files

**New:** `tools/vendor.json`, `tools/fetch_vendor.py`, `THIRD_PARTY.md`.

**Modified:** `.gitignore`; the four target Makefiles
(`nrf_adapter_source_multiceiver/targets/{teensy4x,teensy3x,stm32f103,stm32f030}/Makefile`)
for guards only — no source-list or flag changes; `nrf_adapter_source_multiceiver/README.md`;
`readme_EN.md`; the two plan documents.

**Removed from git, kept on disk:** the five `nrf_adapter_source_multiceiver/Drivers/`
subtrees (583 files, ~11 MB).

**Rewritten:** all history on `GetWorking` and `origin/GetWorking` from `7facf79` onward
(Phase 4). **Deleted:** the `pre-rebase-backup-9a51a05` branch (local only — it was never
pushed to either remote).

**Deliberately untouched:** every `.c`/`.cpp`/`.h` under `core/` and `targets/`,
`targets/esp32/` (its `IDF_PATH` guard is already the model this follows), the Python host
side, and `-DTEENSYDUINO=159`.

## Verification

No firmware source changes, so no hardware run is required — every check below is
build-level.

1. **Manifest matches reality first.** `python3 tools/fetch_vendor.py --check` passes against
   the untouched working tree before Phase 3. This is the gate; a mismatch here means the
   manifest is wrong, not the tree.
2. **Round-trip, per tree.** Move a tree aside, fetch it, `diff -r` against the moved copy,
   confirm only the three documented deltas. Confirm `grep -c 'ARM.extab\*'
   Drivers/teensy4/imxrt1062_t41.ld` is 1 in the fetched copy.
3. **Identical images.** Record `arm-none-eabi-size` and the `objcopy -O binary` output for
   every configuration before the change, then rebuild after a clean re-fetch and compare:
   `BOARD=TEENSY41`, `BOARD=TEENSY40`, `BOARD=TEENSY41 HOST_LINK=HOST_LINK_SERIAL1`,
   `BOARD=TEENSY41 ETH=1`, `BOARD=TEENSY35`, `BOARD=TEENSY36`, stm32f030, stm32f103 CDC,
   stm32f103 `HOST_LINK=HOST_LINK_USART1`. The flash images must match byte-for-byte — not
   as a firmware gate (`[[byte-identical-gate-retired]]`), but because nothing that gets
   compiled changes, so any difference means the fetch script produced the wrong bytes.
   Note the Teensy 3.x image has **one** nondeterministic byte at offset 841 that varies
   between two identically-configured builds; expect it and do not chase it.
4. **Fresh-clone simulation.** Clone the repo to a temp directory, confirm `Drivers/` is
   absent, run `--target stm32f030` alone, confirm *only* CMSIS core + F0 landed (no
   QNEthernet, no cores), and build. Repeat for `--target teensy4x-eth`. This is the
   "download only what you need" requirement, tested rather than asserted.
5. **Guards fire — done and confirmed.** Point each target's tree variable at a nonexistent
   path and confirm the `$(error …)` names the right `--target` invocation. All 8 (2 per
   Teensy target, 3 for stm32f103, 2 for stm32f030) confirmed by direct override
   (`FW_DIR`, `SPI_DIR`, `QNE_DIR`, `CMSIS_INC`, `DEVICE_INC`, `TU_DIR`); an unmodified build
   still succeeds with the guards in place.
6. **`--check` after all of it, plus `git status` clean with all trees present on disk —
   done and confirmed.** `venv/bin/python tools/fetch_vendor.py --check` reports all 8 trees
   `OK` after the Phase 5/6 edits; `git status --short` shows only the intended file changes
   (four target Makefiles, the two READMEs, the two plan docs, the new `THIRD_PARTY.md`) with
   no `Drivers/` paths appearing.
7. **History is actually gone (Phase 4) — done and confirmed.** In the rewritten scratch
   clone: `git log --oneline refs/heads/GetWorking -- nrf_adapter_source_multiceiver/Drivers`
   returned nothing; `git count-objects -vH` / `du -sh` showed the repo shrank from the
   pre-rewrite 73 MB baseline (55 MB after); the five original commit subjects (`add multi
   device support`, `add stm32f103 (blue pill)...`, `add teensy 4.1 support`, `add f103 usb
   cdc port...`, `add control over ethernet...`) were found renamed on `refs/heads/GetWorking`
   with their non-vendor diffstat verified byte-identical to the originals, confirming only
   the vendored paths were stripped, not the commits themselves. Additionally verified (the
   part the original plan missed): `main`, `multi-platform-wip`, and
   `pre-rebase-backup-9a51a05` came out with **identical** `git rev-parse` hashes to the real
   repo, and `git merge-base main GetWorking` still resolved to `main`'s actual tip — proving
   the rewrite's blast radius was exactly the 28 commits unique to `GetWorking` and nothing
   else. After the real push: local `GetWorking` == `origin/GetWorking` (`5a63866`), `git
   status` clean, all five `Drivers/` subtrees still present on disk.
