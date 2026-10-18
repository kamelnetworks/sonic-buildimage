SONiC Kubernetes builder

This builder uses Ubuntu 22.04 userspace and a dedicated Docker daemon inside
one privileged amd64 pod. It does not mount the node's Docker socket, containerd
socket, /dev, or host filesystem. No Kubernetes API token is mounted. Run source
builds as the unprivileged builder user. Privilege is needed for nested containers,
mounts and SONiC image assembly; use a dedicated build namespace and trusted source.

Provision:
  kubectl apply -f tools/kubernetes/builder.yaml
  kubectl wait -n sonic-build --for=condition=Ready pod/sonic-builder --timeout=300s

The manifest requests 4 CPUs / 32 GiB RAM and limits 8 CPUs / 48 GiB RAM.
It uses a 500 GiB ReadWriteOnce PVC through tera-compressed. Change the storage
class and size for other clusters. Worktree, artifacts and Docker layers persist
across pod replacement. Delete only the pod to restart it; deleting the PVC can
remove the complete workspace. Bootstrap package versions follow Ubuntu updates;
record resolved image and package versions when measuring repeatability.

Docker's bridge MTU must match the pod eth0 MTU (1450 in this cluster), otherwise
large transfers inside nested containers can stall. The daemon reads that value
at startup. Keep overlay2 on a filesystem supporting d_type (the tested temporary
workspace is XFS); do not put /var/lib/docker on overlayfs or NFS.

The manifests require the unified cgroup v2 hierarchy to be visible in the
privileged pod. Docker's cgroup parent is set to the Kubernetes pod cgroup so
nested build containers inherit its aggregate CPU and memory limits. Otherwise
Docker can create siblings outside the pod and escape those aggregate limits.
The bootstrap verifies the parent before starting the daemon.

Stage source at /work/src/sonic-buildimage, owned by UID 1000:
  git clone --recurse-submodules --branch kamel/202611-master \
    https://github.com/kamelnetworks/sonic-buildimage.git /work/src/sonic-buildimage

The three modified component branches are published in their Kamel forks. The
supplied migration bundles provide an alternative way to seed their Git objects;
see migration-status.txt for commits and bundles.

Run:
  kubectl exec -n sonic-build sonic-builder -- sudo -u builder bash -lc \
    'cd /work/src/sonic-buildimage; tools/kubernetes/build.sh'

PLATFORM defaults to marvell-teralynx. Pass PLATFORM=broadcom or PLATFORM=mellanox
in the pod environment for other targets. Use a separate PVC, checkout and daemon
for each simultaneous platform build. Do not share writable target directories or
Docker daemons across parallel jobs.

Component example (run inside the pod as builder):
  TARGETS='target/debs/trixie/platform-modules-midstone-200i_0.2.2_amd64.deb target/python-wheels/trixie/sonic_host_services-1.0-py3-none-any.whl' \
    tools/kubernetes/build.sh

Bookworm build passes are disabled for this initial Trixie component validation.
Validate any required Bookworm images separately before treating a full hardware
image as release-ready. Tests remain enabled. INCLUDE_FIPS=n preserves the existing Kamel CI policy
using conventional cryptography packages. SONIC_CONFIG_BUILD_JOBS controls
concurrent packages; SONIC_CONFIG_MAKE_JOBS controls concurrency within each package.
The initial 2 x 4 setting gives large packages four compiler jobs while keeping
total compilation concurrency within the pod's eight-CPU limit.

The script uses SONIC_CONFIG_USE_NATIVE_DOCKERD_FOR_BUILD=y so slave builds use
the pod daemon rather than starting another nested daemon. The Docker socket
shared with the slave belongs only to this pod. This also keeps all build network
traffic behind the correctly sized Docker bridge.

The helper adds --tty through SONIC_BUILDER_EXTRA_CMDLINE. Docker allocates a terminal even
when the outer job runs headlessly, so libyang's Expect-based interactive tests
can run correctly. Without it, Tcl reports closed-stdout errors but its test-file
runner can still return success. Native builds also unmask container system paths
so libnl's user/network namespace tests can mount a fresh read-only sysfs.

The default SONiC slave image build deliberately uses --no-cache. For iterative
local work, append DOCKER_NO_CACHE_FLAG= to the script invocation to reuse Docker
layers; use a fresh build for release validation. Debian artifact caches are keyed
by SONiC build dependencies and persist in /work/cache.

To restore the saved toolchain into a new pod before configuring the source:
  gzip -dc ../build-support/cache/sonic-slave-trixie-727c90ef0e2.tar.gz | \
    kubectl exec -i -n sonic-build sonic-builder -- docker load

The archive checksum and image ID are saved alongside it. It is a build environment
image, not a switch installer. Make verifies the computed slave tag and reuses the
loaded image only while the Dockerfile and pinned inputs match. A cold build is not evidence
of an incremental build speedup; measure both after the first successful image.

preflight.yaml provides a separate temporary 70 GiB workspace to diagnose runtime
and component compilation while persistent storage is unavailable. It is not an
image-build workspace. Deleting that pod deletes its source, logs and build cache.

An additional ephemeral-builder.yaml targets k8s-1 with a 150 GiB local workspace,
750m CPU / 24 GiB memory requests and 8 CPU / 48 GiB memory limits. This is a
cluster-specific fallback when the PVC cannot mount. Check node filesystem space
and existing requests before using it. Kubernetes may evict it under disk pressure;
copy logs and final artifacts out promptly. The persistent manifest remains the
preferred configuration for repeatable builds and incremental caches.
