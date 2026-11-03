# AGENTS.md — kamelnetworks/sonic-buildimage

Kamel Networks' fork of SONiC's image builder. `202611-master` is upstream
sonic-net master pinned at `8ef49ca84` (PR #24) plus our carried patches. The
images run in SONIX production on three platforms. Write claim-limited prose:
say what was observed, on which image (sha256) and which hardware, and keep
unknowns explicit. "Builds" is not the same as "boots", and neither is the
same as "passes traffic".

## Release

The current release is **202611-pre**. Its pinned tracker is #93 and its
milestone is [`202611-pre`](https://github.com/kamelnetworks/sonic-buildimage/milestone/1).
The supported platforms are Celestica Silverstone-X (DS4001, Teralynx 7), Dell
S5232F-ON and Celestica Seastone 2 / HPE 3132Q (both Trident 3). Don't spend
release effort on other platforms.

- `gh issue list -m 202611-pre -l release-blocker` (also `release-should`,
  `needs-hw-validation`) lists the open work.
- Only @bluecmd changes the tracker's scope. Propose changes in a comment on
  the tracker.
- Issues titled `Decision:` are @bluecmd's call. Summarise the options if
  asked, but don't decide, and don't merge code that presumes the answer.

## Layout

| Path | What |
| --- | --- |
| `device/<vendor>/<platform>/` | HWSKUs, `config.bcm`, `pmon_daemon_control.json`. In scope: `x86_64-cel_silverstone-x-r0`, `x86_64-dellemc_s5232f_c3538-r0`, `x86_64-cel_seastone_2-r0`. |
| `platform/broadcom/`, `platform/marvell-teralynx/` | SAI/SDK packaging, platform modules (`sonic-platform-modules-cel`, `-dell`), syncd dockers. |
| `src/` | Submodules. Ours: `sonic-swss`, `sonic-host-services` (caclmgrd), `sonic-linux-kernel`, `sonic-platform-common`, see `.gitmodules`. A fix there is a PR in that repo plus a submodule bump PR here. |
| `files/build_templates/` | Container and service templates (`docker_image_ctl.j2` grants syncd its devices). |
| `.github/workflows/sonic-build.yml` | CI image builds and releases, see `.github/workflows/README.md`. |

## Build & test

- A full image needs hours and tens of GB, so don't build one on a laptop-sized
  machine or in an agent pod. Use one of these:
  - **CI:** `gh workflow run sonic-build.yml -r <branch>`, optionally with
    `-f platforms=marvell-teralynx` or
    `-f create_release=false`. Runners are the in-cluster ARC set
    `sonic-buildimage` (max 3, one build at a time per the concurrency group).
    Releases are `<branch>-v<run>`, tagged `build-<branch>-<run>`.
  - **Builder pods:** `sonic-builder` / `sonic-image-builder` in namespace
    `sonic-build`, through `kubectl exec`. Work under `/work`; logs are in
    `/work/logs/<platform>-image.{log,exit,finished}`. Never delete these
    pods: they hold the build caches.
- Run Python checks inside the slave image (`sonic-slave-trixie`), not on the
  host: `docker run -v /work:/work -e TMPDIR=/work ...`.
- Wait for long builds with a shell sleep loop, not with repeated agent polling.
- Fix ulimit failures with container `--ulimit nproc=-1:-1` / nofile, not by
  changing host limits.
- Static checks and unit tests are fine locally. Say which ones you ran and
  what you didn't run.

## Way of working

- Tickets are GitHub issues. Read the whole issue and every comment before
  starting, and check linked PRs: several issues were already fixed by merged
  PRs.
- **Claim** an issue before working on it:
  `gh issue edit <n> --add-assignee @me`. Don't pick up an issue assigned to
  someone else; ask its assignee instead.
- **Drop** an issue by unassigning yourself and leaving a note with the branch,
  findings and blockers.
- **Stale** claims are ones with no commits, PR or comments for 14 days.
  Comment and @mention the assignee. If there's no reply within 3 working
  days, reassign and say so on the issue. Agents don't reassign on their own;
  flag it to your human.
- **Branches and PRs:** never commit to `202611-master`. Use one issue per
  branch and per PR, named `fix/<issue>-<slug>`, `feat/…`, `docs/…` or
  `test/…`, and opened against `202611-master` with `gh pr create`. Write
  `Closes #<n>` only when the PR completes the issue, and `Refs #<n>` for
  partial work.
- **Merging** needs @bluecmd's approval of that PR or of its class of PRs.
- **New bugs** start with `**Severity: P0–P3** (one-line impact)`, then the
  platform, image and how it was found.
- **Closing:** close an issue only when its acceptance criteria are met, with
  a reason ("fixed in #M", "duplicate of #N", "not reproduced on <image>").
  Tick its box in the release tracker.
- **Fixed but untested:** a merged fix that hasn't been run on hardware gets
  `needs-hw-validation` and stays open until a lab check is posted.
- **Hardware results** are posted to #47 and the issue, as in its existing
  comments. Include:
  - the image name and sha256;
  - the switch and HWSKU;
  - what works, what fails and the exact errors;
  - what you changed by hand (e.g. `/usr/bin/syncd.sh` edits);
  - the state you restored the switch to.
- Never put credentials or tokens in issues, commits, logs or evidence.

## Lab

The SONIX lab is shared with other repos (sonix-network/sai-rev-eng among
them) and uses the same rules. The source of truth is
sonix-network/sonix-ansible: `LAB.md` (topology, consoles, recovery, "Testing
202611" checklist) and `.agents/skills/` (`sonic-onie-install`,
`silverstone-x-breakout`). Read them before touching hardware; this section
doesn't replace them.

| Host | Platform | Use for |
| --- | --- | --- |
| ixp-lab-sw1 | Silverstone-X DS4001 | Teralynx data plane, Silverstone platform API |
| ixp-lab-sw2, sw3 | HPE 3132Q / Seastone 2 | Trident 3, Celestica non-PDDF API, EVPN |
| ixp-lab-sw4 | Dell S5232F-ON | Trident 3, Dell platform API, EVPN |
| ixp-lab-eos1 | Arista 7050QX (EOS) | Known-good hub and reference VTEP, test VMs |
| smoketest | HPE 3132Q | **Not ours.** sai-rev-eng's Nova baseline: don't reimage it or start stock services on it. |

- **Reserve** before changing a device, and put the issue in the message so
  `status` and `history` link back to the work:

  ```sh
  bin/lab-reserve status --json
  bin/lab-reserve take ixp-lab-sw4 --for 2h -m "kamelnetworks/sonic-buildimage#47: 202611-pre checks"
  bin/lab-reserve renew ixp-lab-sw4 --for 1h   # renew as you go, don't take 24h up front
  bin/lab-reserve release ixp-lab-sw4
  ```

  `take` is all-or-nothing. Never `--force` someone else's lease; ask them
  instead.
- **Consoles:** check `bin/lab-reserve consoles` before opening one, and use
  `sercon.py`, which refuses a port that's in use. Don't kill another reader.
- **Authorization:** carry forward the authorization you were given, but a
  reinstall, reboot, power cycle or HWSKU change (which reboots) needs its own
  explicit OK. A runbook is not authorization.
- **Before and after:** record the switch state before you change it
  (`show version`, running config, `systemctl --failed`). Restore it afterwards
  and say in the report whether the restore worked.
- **Ansible:** always run it with an explicit `-l` limit.
- **Reachability:** the lab is only reachable through `oob-rl-sw1` (Pomerium).
  Agent pods without lab network access can't do hardware work; hand it to a
  session that has access.
