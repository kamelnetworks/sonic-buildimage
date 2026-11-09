#  Github actions README
This is an introduction about auto-cherry-pick workflow.
take 202205 branch for example:
1. pr_cherrypick_prestep:
```mermaid
graph
Start(Origin PR) --> A{merged?}
A -- NO --> STOP
A -- YES --> A1{Approved<br> for 202205<br> Branch?}
A1 -- NO --> STOP
A1 -- YES --> A2(pr_cherrypick_prestep)
B(pr_cherrypick_prestep)
B --> B1{cherry pick<br>conflict?}
B1 -- YES --> B2(Add tag:<br>Cherry Pick Confclit_202205) --> B3(Add comment:<br>refer author code conflict) --> STOP1(STOP)
B1 -- NO --> B4(Create New PR) -- success --> B5(New PR add tag:<br> automerge) --> B6(New PR add comment:<br>Origin PR link) --> B7(Origin PR add tag:<br>Created PR to 202205 Branch) --> B8(Origin PR add comment:<br>New PR link)
B4 -- fail --> STOP1
```

2. automerge:
```mermaid
graph
Start(PR azp finished successfully) --> A{author:<br>mssonicbld?}
A -- NO --> STOP
A -- YES --> B{tag:<br>automerge?} -- YES --> C(Merge PR)
B -- NO --> STOP
```

3. pr_cherrypick_poststep:
```mermaid
graph
A(PR is Merged) --> B{tag:<br>automerge?}
B -- YES --> B1{author:<br>mssonicbld?}
B1 -- YES --> B2{"title starts:<br>[action] [PR:123]"}
B2 -- YES --> C(Origin PR remove tag:<br> Created PR to 202205 Branch) --> D(Origin PR add tag:<br> Included in 202205 Branch)
B -- NO --> STOP
B1 -- NO --> STOP
B2 -- NO --> STOP
```

## Build releases

`sonic-build.yml` publishes only after VS, Broadcom, and Teralynx all build
successfully and all recorded release assets are present. The release title
is `<branch>-v<run_number>`; its tag is `build-<branch>-<run_number>`.

GitHub release assets must be smaller than 2 GiB. Larger files are split into
1,900 MiB numbered parts, with a `.parts` file listing their order and a
`.sha256` file containing the original file's checksum. Download all matching
parts and both metadata files into one directory, then reconstruct the file:

```sh
cat $(cat sonic-vs.bin.parts) > sonic-vs.bin
sha256sum -c sonic-vs.bin.sha256
```

The reconstructed installer is byte-for-byte identical to the build output.
Files below the limit retain their existing asset names.

## Fork unit tests

`fork-unit-tests.yml` runs on every pull request and on pushes to
`202611-master`. It calls `scripts/run-fork-unit-tests.sh`, which finds the
tests by where they live, so a new test needs no workflow change:

| Location | Files | Run with |
| --- | --- | --- |
| `device/<vendor>/<platform>/tests/` | `test_*.py` | `python -m pytest` |
| `platform/**/tests/` | `test_*.py` | `python -m pytest` |
| `tests/` | `test_*.py` | `python -m pytest` |
| `files/**/tests/` | `test_*.sh` | `bash` |

- Only files directly in a `tests/` directory count, and each file runs in its
  own process. A file fails the job if it fails or collects no tests.
- The tests run on Python 3.13 (as in `sonic-slave-trixie`) without a build and
  without hardware. Mock sysfs, IPMI and STATE_DB in the test.
- `PYTHONPATH` holds the code under test (the directory containing `tests/`,
  or the first non-package directory above it), `src/sonic-py-common` and the
  pinned `src/sonic-platform-common` and `src/sonic-platform-daemons`
  (`sonic-xcvrd`). The script checks them out shallow at the pinned commits. If
  a test needs another submodule, add it to `SUBMODULES`/`PYPATH` in the
  script. PyPI packages go in `scripts/fork-unit-tests-requirements.txt`.
- A test that needs build output (swsscommon, libyang, vendor SDKs) must not be
  named `test_*.py`. For example, `tests/dpb/config_dpb_platforms_test.py` runs
  inside a sonic-utilities tree, as its README describes.
- Upstream suites that need such a build are listed in `EXCLUDE` in the
  script, and each entry says why.

Run it locally the same way, in a `python:3.13` container:

```sh
pip install -r scripts/fork-unit-tests-requirements.txt
scripts/run-fork-unit-tests.sh          # --list shows what would run
```


## Persistent compiler cache (opt in)

For `sonic-build.yml`, set `compiler_cache=true` to reuse C/C++ compilation
results when a source package changes and its whole-package cache misses.
The existing writable `cache_source` is mounted into the slave at `/dpkg_cache`;
ccache uses `.ccache-<build UID>/<build environment>/<architecture>` there, with a 5 GB
limit per directory. UID separation avoids ownership conflicts between builders.
It survives runner pods and `target/` cleanup. Move this
cache source with the runner PVC; no node-local disk is required.

An existing rules/config.user is preserved, so this input does not override it.
The default remains off pending component-CI measurements. With `cache_mode=none`
or an unavailable cache source, this option stays disabled. Package caching,
compiler flags, compiler-content checking and ccache's normal header/source
validation remain unchanged. The job prints hit/miss statistics after a successful
build. A warm single-object test demonstrates reuse, not an under-one-hour full
image build; compare representative changed-package CI runs before claiming that.
