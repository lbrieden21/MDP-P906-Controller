# Replace `nrf_adapter_source/` with the bare-metal rewrite

## Context

The repo carries two firmware trees. `nrf_adapter_source/` is the STM32CubeMX +
HAL + Keil MDK project this repo shipped with, imported whole in the `init`
commit and never touched since. `nrf_adapter_source_multiceiver/` is the
bare-metal rewrite that has since absorbed every piece of ongoing work: five
target families, three host-link transports, the pipe-addressing commands
(`CMD_NRF_OPEN_PIPE` 0x23, `CMD_NRF_SET_TX_TARGET` 0x24) and the pipe-tagged
`NRF_RECV_OK` frame.

The old tree is now **non-functional against the rest of the system**:
`mdp_controller/bus.py` opens a hardware RX pipe per device unconditionally, even
for a single device, and the shipped firmware has no such command. Keeping a dead
232-file tree around — 116 of them committed Keil build artifacts, 82 vendored
HAL/CMSIS with an admittedly incomplete licensing story (`THIRD_PARTY.md:51-58`)
— costs half the repo's tracked file count for nothing.

Outcome: the old tree is deleted, the rewrite takes over the `nrf_adapter_source`
name, and every reference across the repo points at that one directory. Only the
root English readme records that a rewrite happened; everywhere else reads as if
the directory has always been what it now is.

## Decisions already taken

| Question | Decision |
|---|---|
| `plans/` docs (~50 refs) | **Leave untouched.** Dated how-we-got-here records; the paths were correct when written, and `devendoring_plan.md` contains literal `git log`/`git-filter-repo` commands that only work against the old path. |
| Provenance comments citing the old tree | **Upstream-repo attribution only** — cite `ElluIFX/MDP-P906-Controller` / `mokhwasomssi/stm32_hal_nrf24l01p`, never an in-repo path. |
| Git history | **Plain delete, no purge.** The tree arrived in the root `init` commit; a `git-filter-repo` pass would rewrite all 127 commits, invalidate both remotes, and erase the upstream import for ~8.9 MB. |

## Phase 1 — Remove and rename

```sh
git rm -r --quiet nrf_adapter_source
rm -rf nrf_adapter_source                                    # untracked .vscode/ left behind by git rm
rm -rf nrf_adapter_source_multiceiver/targets/esp32/build    # see note
git mv nrf_adapter_source_multiceiver nrf_adapter_source
```

Notes:

- `git rm -r` leaves `nrf_adapter_source/.vscode/` on disk (untracked, gitignored,
  and already pointing at nonexistent PlatformIO paths). The `rm -rf` clears it so
  the destination name is free.
- `git mv` on a directory does a filesystem rename, so the **gitignored
  `Drivers/` tree (11 MB, already fetched) comes along** — no re-fetch needed —
  as do untracked `pipe_test.py` and `.env`.
- **`targets/esp32/build/` is 1.1 GB across 13 board configs, and every
  `CMakeCache.txt` in it hard-codes the old absolute source path.** After the
  rename CMake refuses to reuse them and demands a fullclean, so they are dead
  weight either way; deleting reclaims the space. Deferring this is fine — it
  only means the first `make BOARD=...` per board fails until fullcleaned — but
  the dirs cannot survive the rename in usable form.

## Phase 2 — Mechanical path updates

Rewrite `nrf_adapter_source_multiceiver` → `nrf_adapter_source` in these files.
The old name is a strict superset of the new one, so a single substitution is
unambiguous and self-terminating.

| File | Refs | Note |
|---|---|---|
| `.gitignore` (lines 26-31) | 6 | **Do this in the same change as the rename** — otherwise the 11 MB of fetched `Drivers/` stop being ignored and show up staged-able in `git status`. |
| `tools/vendor.json` | 43 | All are `"to":` destination paths. |
| `tools/fetch_vendor.py` (line 3) | 1 | Module docstring. |
| `THIRD_PARTY.md` (7, 11, 80) | 3 | See Phase 5 — that file needs more than a path swap. |
| `readme_EN.md` | 8 | See Phase 4 — same. |
| `nrf_adapter_source/persistence_test.py` (280, 313) | 2 | Self-referencing help text it prints to the operator. |
| `mdp_controller/bus.py` (25-26) | 1 | **Also fix a path that is wrong today:** it cites `.../Core/Src/nrf24l01p.c`, but the file is at `core/nrf24l01p.c` — `Core/Src/` was the *old* tree's layout. |

Nothing else needs touching: there are **zero relative-path crossings** between
the two trees (no `#include`, `VPATH`, `add_subdirectory`, or linker path), so
both are build-independent. `.github/workflows/` and `readme.md` (Chinese) have
no references to either name.

## Phase 3 — Provenance comments

13 comments in the firmware tree cite the old tree by repo path. After the rename
those paths resolve to nothing and read as self-references. Rewrite them to name
the upstream project instead of an in-repo path.

**Pattern:** `nrf_adapter_source/Core/Src/spi.c` → `the shipped firmware's
Core/Src/spi.c`. Keep the filename (it's the upstream file's own path and stays
useful for lookup); drop the directory prefix that would read as this tree.
Name the upstream slug only where it anchors — `README.md` and
`targets/stm32f030/gpio.h` — and let the per-peripheral comments say "the shipped
HAL firmware" so they stay one line.

| Site | Becomes |
|---|---|
| `README.md:3` | "Replaces the shipped adapter firmware from `ElluIFX/MDP-P906-Controller` (STM32CubeMX + HAL + `mokhwasomssi/stm32_hal_nrf24l01p`, single nRF24 RX pipe) with a bare-metal firmware…" |
| `README.md:39` | "`Modules/nrf24l01/nrf24l01p.c` in the shipped firmware (`mokhwasomssi/stm32_hal_nrf24l01p`) only ever writes `RX_ADDR_P0`…" |
| `README.md:45-47` | Lead-in gains the slug once — "…already recovered from the shipped firmware source (`ElluIFX/MDP-P906-Controller`), not reverse-engineered:" — which then covers the bare `MDP_Adapter.ioc` / `Core/Src/main.c` / `Core/Inc/main.h` filenames in the bullets at 47, 60, 63, 67 that are already unqualified today. |
| `core/nrf24l01p.h:5` | "Bare-metal port of the shipped firmware's `Modules/nrf24l01/nrf24l01p.c`" — line 6 already names `mokhwasomssi/stm32_hal_nrf24l01p`, so only the prefix drops. |
| `core/protocol.h:6` | "ported from the shipped firmware's `Core/Src/main.c` (handle_uart_command/parse_uart_data)" |
| `targets/stm32f030/gpio.h:22` | "Pin mapping, recovered from the shipped HAL firmware's `Core/Inc/main.h` (`ElluIFX/MDP-P906-Controller`)" |
| `gpio.c:5`, `spi.c:6`, `uart.c:7`, `watchdog.c:4`, `system_clock.c:6` | "Reproduces the shipped HAL firmware's `Core/Src/<file>`…" — body text unchanged. |
| `flash_store.c:7` | "…the shipped firmware's MiniFlashDB-backed settings (`Modules/MiniFlashDB`)" |
| `startup.c:47` | "Vector order recovered from the shipped HAL firmware's `MDK-ARM/startup_stm32f030x6.s`" |

Keep the README's existing "Status" and "Why bare-metal instead of extending the
HAL library" framing as-is otherwise — the user's brief is that it needs to say
what was taken and why the old firmware was replaced, which it already does.

## Phase 4 — `readme_EN.md`

Two jobs. First the 8 path/link swaps from Phase 2 (lines 13, 58, 60, 76, 88, 96,
118, 141).

Second, **this is the one file that records the rewrite.** The existing "Hardware
note" block (lines 9-18) already carries the lineage story and is the right home.
Add to it, roughly:

> `nrf_adapter_source/` no longer holds the STM32CubeMX + HAL + Keil MDK project
> this repo originally shipped with — it was completely rewritten as a bare-metal
> (direct CMSIS register access) multi-target firmware under the same directory
> name. The original remains in this repository's git history at the `init`
> commit.

Leave `readme.md` (Chinese) alone — it has no references to either name, and it
is never edited in this repo.

## Phase 5 — `THIRD_PARTY.md`

More than a path swap:

- Line 7 heading and line 11 → new path.
- **Delete lines 51-58 entirely** (the `## nrf_adapter_source/Drivers/` section).
  That tree no longer exists in the working tree, and leaving it would collide
  with the renamed heading at line 7. Line 57's "the way the multiceiver tree's
  can" comparison goes with it.
- Line 80 → new path. The closing "What this file doesn't cover" section stays
  accurate as written: it disclaims git history, and this file inventories the
  working tree.

## Out of scope

- `plans/` — 9 files, ~50 refs, untouched by decision.
- `MDP_Adapter_Multiceiver` — the firmware's build-artifact name (`TARGET` in 5
  Makefiles, `project()` in `targets/esp32/CMakeLists.txt`, and the flash commands
  in both READMEs). It names the firmware, not the directory, and it is still
  accurate. Renaming it would invalidate every documented `st-flash`/
  `teensy_loader_cli` line for no gain.
- Git history rewrite.
- `readme.md` (Chinese).

## Verification

1. **No stragglers** — expect zero hits:
   ```sh
   grep -rIn -i "nrf_adapter_source_multiceiver" --exclude-dir=.git \
     --exclude-dir=venv --exclude-dir=plans --exclude-dir=build .
   ```
2. **Rename registered as a rename, and `Drivers/` still ignored:**
   ```sh
   git status            # 232 deletions, 83 renames; NO Drivers/ under "untracked"
   git ls-files nrf_adapter_source | wc -l    # 83
   ```
   `Drivers/` appearing as untracked means the `.gitignore` edit was missed.
3. **Vendor manifest still resolves** — checks every `"to":` path against disk,
   downloads nothing:
   ```sh
   venv/bin/python tools/fetch_vendor.py --check
   ```
4. **Builds clean from the new path** — proves no target depended on the old
   directory name. STM32F030 is the primary board; teensy4x additionally exercises
   the moved `Drivers/` tree:
   ```sh
   cd nrf_adapter_source/targets/stm32f030 && make clean && make
   cd ../teensy4x && make clean && make
   ```
   Both should emit `build/MDP_Adapter_Multiceiver.{elf,hex,bin}`.
5. **ESP32** (Phase 1's build wipe should have already been done, if not do it — this is a full IDF rebuild,
   several minutes): `cd nrf_adapter_source/targets/esp32 && make BOARD=esp32c6`.
6. **Host side** — `mdp_controller/bus.py`'s change is comment-only, so
   `venv/bin/python -c "import mdp_controller"` is sufficient; no behavioural
   change to test. `test_main.py` needs real hardware and is not required here.
7. **Optional hardware smoke test**, if a board is flashed from step 4:
   `venv/bin/python nrf_adapter_source/host_link_test.py --port <tty>`.

## Handoff

Do not commit, stage , reset, reflog or push, no destructive git — user handles that stuff, just tell what needs to be done. Like all work, this is a full cutover, no compatibility shims are deprecating, just yank the bandaid and get it done.
