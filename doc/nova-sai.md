# Opt-in Nova SAI artifact for TD3 lab switches

[sonix-network/nova-sai](https://github.com/sonix-network/nova-sai) provides an
experimental Rust SAI skeleton, a Rust kernel module with a minimal C
registration bridge, and a Debian replacement package. **It does not forward
packets. All SAI switch/port state is fictitious.** This is intended for isolated
TD3 platform development, not a production image or a functional syncd alternative.

The implementation is in [Nova PR #3](https://github.com/sonix-network/nova-sai/pull/3);
the helper pins commit `5cdc31697d71ed3cb15738e92aa390d76eeab3b4`.

The stock kernel configuration remains unchanged: CONFIG_RUST stays disabled
and CONFIG_MODVERSIONS stays enabled. This avoids breaking existing Broadcom
module binaries. The Rust module uses the stock C kernel ABI and version checks;
its core is no_std Rust with owned initialization/Drop teardown. It does not bind
PCI devices, access registers, or use DMA. Native Rust migration is deferred.

## Build only

Supply the matching SONiC header/common/kbuild packages in target/debs/trixie,
then run on Linux x86_64 with Bazelisk, dpkg-deb, Make, binutils and modinfo:

```sh
scripts/build-nova-sai
```

The helper fetches a pinned Nova source revision and invokes its Bazel package
target. Bazel builds Rust core with kernel mitigation flags, uses the matching
GCC 14 and SONiC Kbuild/Module.symvers, compiles the userspace service and library,
and assembles the .deb. The helper copies the result and a SHA-256 checksum into
target/debs/trixie. It never installs packages, changes an image, or activates a
replacement. Use --source for an explicit local Nova checkout, --kernel-debs for
a different package directory, or --output for a different artifact directory.
No package is added to default image contents and no default feature changes.

The initial release targets the exact ixp-lab-sw2 kernel build observed on
2026-10-24: 6.12.41+deb13-sonic-amd64, SONiC commit 41d1fd836, kernel submodule
b15abd62f89343be629d62f66b060a0ed2f12a0d. SDK package hashes are pinned in
Nova's kernel/target.json. A package hash mismatch fails the build; it must not be
worked around by substituting unrelated Debian headers. Retarget and validate
Nova against the actual new SDK when rebuilding a different kernel/image.

## Deployment deliberately replaces the running stack

Read the package's README.install and target.json before deploying:

```sh
dpkg-deb --info target/debs/trixie/nova-sai_0.1.0_amd64.deb
dpkg-deb --extract target/debs/trixie/nova-sai_0.1.0_amd64.deb /tmp/nova-inspect
```

On a matching single-ASIC Broadcom TD3 SONiC 202611-master lab host, installation
with apt activates the replacement. Its preflight checks the kernel release,
configuration fingerprint, platform and module hash. Activation disables and
stops syncd/SWSS, masks their units and the vendor module unit, normally unloads
named Broadcom ASIC modules, installs module loading gates, updates the initramfs,
and starts the mock Nova service. Management NIC/board drivers and vendor packages
are preserved. Forwarding stops. Keep console access for runtime replacement.

The package records the prior feature/service/module state and attempts rollback
on activation failure. Restore it with `sudo nova-sai-control deactivate` or
remove the package (which restores recorded state before removing files).

Validation in Nova includes a C ABI test, a live Thrift/shell test, Rust core
lifetime tests, mocked activation/rollback tests, and loading/unloading its .ko
in QEMU with the actual stock SONiC kernel. The SAI library communicates with
the Rust driver through a versioned `/dev/nova` ioctl interface. Physical
installation, SAI operations through the driver, removal/restoration and reinstall
passed on the isolated TD3 smoketest switch; see the
[report](https://github.com/sonix-network/nova-sai/blob/5cdc31697d71ed3cb15738e92aa390d76eeab3b4/docs/test/smoketest/nova-install-2026-10-24.md).
The stock forwarding stack was already disabled on that lab switch. Run this helper's tests with:

```sh
python3 -m unittest discover -s scripts/tests -p 'test_build_nova_sai.py'
```
