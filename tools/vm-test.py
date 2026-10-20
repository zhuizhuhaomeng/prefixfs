#!/usr/bin/env python3
"""Test prefixfs only inside a disposable, network-free QEMU guest."""
import argparse
import gzip
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile

ROOT = Path(__file__).resolve().parent.parent


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def build_guest(directory, module, release, tests):
    guest = directory / 'root'
    guest.mkdir()
    for name in ('dev', 'proc', 'sys', 'tmp', 'run', 'ram', 'source', 'views', 'bin', 'etc'):
        (guest / name).mkdir()
    (guest / 'tmp').chmod(0o1777)
    copied = set()

    def copy_binary(source):
        source = Path(source).absolute()
        if str(source) in copied:
            return
        copied.add(str(source))
        target = guest / str(source).lstrip('/')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=True)
        with source.open('rb') as file:
            elf = file.read(4) == b'\x7fELF'
        if not elf:
            return
        result = subprocess.run(['ldd', str(source)], capture_output=True, text=True)
        if 'not found' in result.stdout:
            raise RuntimeError('missing dependency for ' + str(source))
        for dependency in re.findall(r'(/[\w./+\-]+)', result.stdout):
            if Path(dependency).is_file():
                copy_binary(dependency)
                # Host ld.so.cache may resolve /usr/local or /opt libraries.
                # Provide the same SONAME in the guest's default search path.
                alias = guest / 'lib64' / Path(dependency).name
                if not alias.exists():
                    alias.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dependency, alias)

    for program in ('/bin/sh', shutil.which('mount'), shutil.which('umount'),
                    shutil.which('insmod'), shutil.which('rmmod'), shutil.which('modprobe'),
                    sys.executable):
        if not program:
            raise RuntimeError('mount, umount and kmod tools are required')
        copy_binary(program)
    python = sys.executable
    for source in {sysconfig.get_path('stdlib'), sysconfig.get_path('platstdlib')}:
        target = guest / source.lstrip('/')
        shutil.copytree(source, target, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('site-packages', '__pycache__',
                                                     'ensurepip', 'test', 'tests'))
        for extension in Path(source).glob('lib-dynload/*.so'):
            copy_binary(extension)
    for name in ('ext4', 'virtio_blk', 'virtio_rng'):
        result = run('modprobe', '--show-depends', '-S', release, name,
                     capture_output=True, text=True)
        for line in result.stdout.splitlines():
            if line.startswith('insmod '):
                source = Path(shlex.split(line)[1])
                target = guest / str(source).lstrip('/')
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
    for source in (Path('/lib/modules') / release).glob('modules.*'):
        if source.is_file():
            target = guest / str(source).lstrip('/')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    shutil.copy2(module, guest / 'prefixfs.ko')
    shutil.copy2(ROOT / 'tests/functional.py', guest / 'tests.py')
    (guest / 'prefixfs-vm-marker').touch()
    (guest / 'etc/passwd').write_text('root:x:0:0:root:/:/bin/sh\n')
    (guest / 'etc/group').write_text('root:x:0:\n')
    (guest / 'etc/mtab').symlink_to('/proc/self/mounts')
    helper = directory / 'uid.c'
    helper.write_text('#include <unistd.h>\nint main(void) { return geteuid() == 0 ? 42 : 0; }\n')
    run('cc', '-O2', str(helper), '-o', str(directory / 'pf-uid'))
    copy_binary(directory / 'pf-uid')
    shutil.copy2(directory / 'pf-uid', guest / 'bin/pf-uid')
    arguments = ' '.join(shlex.quote(test) for test in tests)
    init = f'''#!/bin/sh
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH PYTHONDONTWRITEBYTECODE=1
mount -t devtmpfs devtmpfs /dev
exec </dev/console >/dev/console 2>&1
mount -t proc proc /proc
mount -t sysfs sysfs /sys
finish() {{
    result=$?
    trap - EXIT
    if [ -d /sys/module/prefixfs ]; then
        rmmod prefixfs || result=1
    fi
    if [ "$result" = 0 ]; then
        echo PREFIXFS_VM_RESULT=PASS
    else
        echo PREFIXFS_VM_RESULT=FAIL
    fi
    {shlex.quote(python)} -c 'import ctypes,os; os.sync(); ctypes.CDLL(None).reboot(0x4321fedc)'
    while :; do :; done
}}
trap finish EXIT
set -e
echo 30 >/proc/sys/kernel/hung_task_timeout_secs
echo 1 >/proc/sys/kernel/hung_task_panic
modprobe virtio_blk
modprobe virtio_rng
modprobe ext4
mount -t ext4 /dev/vda /source
mount -t tmpfs -o size=256m tmpfs /ram
insmod /prefixfs.ko
{shlex.quote(python)} /tests.py {arguments}
'''
    (guest / 'init').write_text(init)
    (guest / 'init').chmod(0o755)
    return guest


def write_initramfs(guest, destination):
    sequence = 0
    with destination.open('wb') as raw, gzip.GzipFile(fileobj=raw, mode='wb', mtime=0) as out:
        def entry(name, mode, payload=b'', major=0, minor=0):
            nonlocal sequence
            sequence += 1
            encoded = os.fsencode(name) + b'\0'
            fields = (sequence, mode, 0, 0, 1, 0, len(payload), 0, 0, major, minor,
                      len(encoded), 0)
            header = b'070701' + b''.join(('%08x' % value).encode() for value in fields)
            out.write(header + encoded)
            out.write(b'\0' * (-(len(header) + len(encoded)) % 4))
            out.write(payload)
            out.write(b'\0' * (-len(payload) % 4))
        for path in sorted(guest.rglob('*')):
            info = path.lstat()
            name = str(path.relative_to(guest))
            if stat.S_ISLNK(info.st_mode):
                entry(name, info.st_mode, os.fsencode(os.readlink(path)))
            elif path.is_dir():
                entry(name, info.st_mode)
            else:
                entry(name, info.st_mode, path.read_bytes())
        entry('dev/console', stat.S_IFCHR | 0o600, major=5, minor=1)
        entry('TRAILER!!!', 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kernel', type=Path, required=True)
    parser.add_argument('--qemu', default=shutil.which('qemu-system-x86_64')
                        or shutil.which('qemu-kvm')
                        or ('/usr/libexec/qemu-kvm' if Path('/usr/libexec/qemu-kvm').is_file() else None))
    parser.add_argument('--timeout', type=int, default=240)
    parser.add_argument('--test', action='append', default=[], help='unittest name, repeatable')
    args = parser.parse_args()
    if not args.qemu:
        parser.error('QEMU is required; pass --qemu /path/to/qemu-system-x86_64')
    if not args.kernel.is_file():
        parser.error('kernel image not found')
    module = ROOT / 'prefixfs.ko'
    release = run('modinfo', '-F', 'vermagic', str(module), capture_output=True,
                  text=True).stdout.split()[0]
    (ROOT / 'build').mkdir(exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='vm-', dir=ROOT / 'build'))
    print('VM artifacts: ' + str(directory), flush=True)
    guest = build_guest(directory, module, release, args.test)
    image = directory / 'source.img'
    with image.open('wb') as file:
        file.truncate(128 * 1024 * 1024)
    run('mkfs.ext4', '-q', '-F', str(image))
    initramfs = directory / 'initramfs.cpio.gz'
    write_initramfs(guest, initramfs)
    command = [args.qemu, '-machine', 'q35,accel=tcg', '-cpu', 'max', '-smp', '2',
               '-m', '768', '-display', 'none', '-serial', 'stdio', '-monitor', 'none',
               '-nodefaults', '-no-reboot', '-nic', 'none', '-kernel', str(args.kernel),
               '-initrd', str(initramfs), '-append',
               'console=ttyS0 rdinit=/init panic=1 oops=panic nokaslr selinux=0 crashkernel=0',
               '-drive', 'file=' + str(image) + ',format=raw,if=virtio',
               '-device', 'virtio-rng-pci']
    log = directory / 'console.log'
    print('Booting isolated guest with ' + release, flush=True)
    timed_out = False
    with log.open('w') as output:
        try:
            result = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT,
                                    timeout=args.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            result = None
    text = log.read_text(errors='replace')
    print('\n'.join(text.splitlines()[-100:]))
    passed = ('PREFIXFS_VM_RESULT=PASS' in text and not timed_out
              and result is not None and result.returncode == 0
              and not re.search(r'BUG:|WARNING: CPU:|Kernel panic|Oops:', text))
    print('VM result: ' + ('PASS' if passed else 'FAIL') + '; console: ' + str(log))
    return 0 if passed else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        raise SystemExit('vm-test: ' + str(error))
