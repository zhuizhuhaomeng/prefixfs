# prefixfs

An experimental Linux kernel filesystem that routes selected first-level
**directory-name prefixes** to a supplied tmpfs directory. Other names use
the source directory. File I/O calls the native backing filesystem directly;
there is no FUSE daemon or userspace request queue.

This is a new standalone project. It does not replace prefix-tmpfs or alter
Test::Nginx. Build and test it in a disposable VM before considering deployment.

## Build

```
make KDIR=/lib/modules/$(uname -r)/build
```

The first target is Rocky Linux 9's `5.14.0-611.47.1.el9_7.x86_64` kernel and
its matching development headers. That kernel backports the `mnt_idmap` VFS
API. Kernel module APIs are not stable; other kernel builds need validation.

## Mount

The following commands are for a disposable test machine, as root:

```
mkdir -p /source /memory /view
mount -t tmpfs -o size=512m tmpfs /memory
insmod ./prefixfs.ko
mount -t prefixfs /source /view \
    -o memory=/memory,prefix=servroot,prefix=cache_
```

`/view/servroot_123/` and `/view/cache_work/` use `/memory`. Ordinary files,
non-matching directories and prefix-matching regular files/symlinks use
`/source`. Existing source directories whose names match a prefix are hidden;
their data is not copied or removed. Prefixes are literal, not glob patterns.
No prefix is implied. Repeat `prefix=` to configure multiple prefixes.

Rename and hard-link operations within a backend use native VFS operations.
Cross-backend operations return `EXDEV`, including exchanges whose reverse
move would change routing. Unix socket endpoints must be bound
and accessed through the same view. Unmounting never deletes either backing
tree; tmpfs data remains available for debugging until explicitly removed
or the backing tmpfs is unmounted. Like ordinary tmpfs, it may use swap if
swap is enabled.

```
umount /view
# Inspect /memory and /source before any explicit cleanup.
rmmod prefixfs
```

## Scope and constraints

- Mounting requires administrative privileges in the initial user namespace.
- The memory backend must really be tmpfs. Backing trees must be disjoint.
- Perform namespace and content mutations through the mounted view while it is
  active; direct concurrent mutation of the backing trees is unsupported.
- Backing submounts and idmapped backing mounts are not supported.
- Device access, privileged executable bits, file capabilities, filesystem
  freezing, whiteout rename and anonymous temporary files are not supported.
  Ordinary executable files remain usable when the backing mount permits it.
- This first implementation needs filesystem and SELinux policy review before
  production use. VM tests validate functional behavior, not production safety.

## Development

Requirements: matching kernel development headers and installed kernel modules,
GCC, make, Python 3, kmod, util-linux, e2fsprogs and QEMU with x86 TCG support.
No Python packages are required. The runner creates an initramfs from public
host binaries/libraries, boots without networking, uses a fresh ext4 disk image
and tmpfs, and loads the module only inside that guest. SELinux is disabled in
the guest; enforcing-policy behavior is not validated.

```
make test QEMU=/usr/libexec/qemu-kvm
# Or choose a matching development tree, kernel image and QEMU explicitly:
make test KDIR=/path/to/kernel/build KERNEL=/path/to/vmlinuz QEMU=/path/to/qemu
```


`make test` builds and boots a disposable QEMU guest. It does not load the
module into the host kernel. See `tools/vm-test.py --help` for kernel and QEMU
selection. Test artifacts remain under `build/` for debugging.

The implementation follows the kernel's [VFS interfaces](https://docs.kernel.org/filesystems/vfs.html).
Unlike [OverlayFS](https://docs.kernel.org/filesystems/overlayfs.html), it selects
one writable backend per routed subtree and does not perform copy-up.

The functional suite covers routing, mounting over the source path, native
metadata and I/O, preallocation, mmap, sendfile, copy_file_range, directory
cookies, rename/exchange, hard links, FIFO, Unix sockets, ACL/xattrs, locks,
concurrent updates, read-only/noexec enforcement, invalid mounts, busy unmount
and retained debug data. Performance comparisons need a native workload;
TCG functional tests are not a throughput benchmark.

## Verified baseline

On 2026-10-20 the module built with `W=1` against Rocky Linux
`5.14.0-611.47.1.el9_7.x86_64` headers. All 42 functional tests passed in a
QEMU TCG guest running that exact kernel with ext4 as the source and tmpfs as
the memory backend. Module unload completed without a kernel BUG, warning or
Oops. The host module was not loaded. BTF generation was skipped because the
build environment has no matching `vmlinux` image.

SELinux enforcing mode, other kernel releases, native throughput and long-term
stress behavior remain unvalidated.
