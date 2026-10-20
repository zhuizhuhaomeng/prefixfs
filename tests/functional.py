#!/usr/bin/env python3
"""Run only inside the disposable VM created by tools/vm-test.py."""
import ctypes
import errno
import fcntl
import json
import mmap
import os
from pathlib import Path
import shutil
import socket
import stat
import struct
import subprocess
import unittest


def command(*args, check=True):
    return subprocess.run(args, check=check, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True)


class PrefixFSTests(unittest.TestCase):
    def setUp(self):
        self.name = self._testMethodName
        self.lower = Path('/source') / self.name
        self.memory = Path('/ram') / self.name
        self.view = Path('/views') / self.name
        self.mounted = False
        for path in (self.lower, self.memory, self.view):
            path.mkdir()
            path.chmod(0o777)
        self.addCleanup(self.cleanup)
        self.mount()

    def mount(self, extra=''):
        options = 'memory=' + str(self.memory) + ',prefix=servroot,prefix=cache_,prefix=build_'
        command('mount', '-t', 'prefixfs', str(self.lower), str(self.view), '-o', options + extra)
        self.mounted = True

    def unmount(self):
        command('umount', str(self.view))
        self.mounted = False

    def cleanup(self):
        if self.mounted:
            self.unmount()
        for path in (self.view, self.lower, self.memory):
            shutil.rmtree(path)

    def directory(self, name='servroot_a'):
        path = self.view / name
        path.mkdir()
        return path

    def assert_errno(self, expected, function, *args, **kwargs):
        with self.assertRaises(OSError) as failure:
            function(*args, **kwargs)
        self.assertEqual(failure.exception.errno, expected, str(failure.exception))

    def child_access(self, path):
        read, write = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read)
            try:
                os.setgroups([])
                os.setgid(1000)
                os.setuid(1000)
                Path(path).read_bytes()
                result = 0
            except OSError as error:
                result = error.errno
            os.write(write, str(result).encode())
            os._exit(0)
        os.close(write)
        result = int(os.read(read, 64))
        os.close(read)
        self.assertEqual(os.waitpid(pid, 0)[1], 0)
        return result

    def test_routing(self):
        for name in ('servroot_a', 'cache_one', 'build_work'):
            path = self.directory(name) / 'value'
            path.write_bytes(b'memory data')
            self.assertEqual((self.memory / name / 'value').read_bytes(), b'memory data')
            self.assertFalse((self.lower / name).exists())
        data = self.directory('data') / 'value'
        data.write_bytes(b'disk data')
        self.assertEqual((self.lower / 'data/value').read_bytes(), b'disk data')
        self.assertFalse((self.memory / 'data').exists())

    def test_matching_regular_files_stay_on_source(self):
        for name in ('servroot.log', 'cache_file', 'build_binary'):
            (self.view / name).write_text(name)
            self.assertEqual((self.lower / name).read_text(), name)
            self.assertFalse((self.memory / name).exists())

    def test_prefix_only_applies_at_root(self):
        path = self.directory('data') / 'servroot_nested'
        path.mkdir()
        (path / 'value').write_text('nested disk data')
        self.assertEqual((self.lower / 'data/servroot_nested/value').read_text(), 'nested disk data')
        self.assertEqual(list(self.memory.iterdir()), [])

    def test_source_directories_hidden_and_preserved(self):
        self.unmount()
        (self.lower / 'servroot_old').mkdir()
        (self.lower / 'servroot_old/old').write_text('preserve')
        self.mount()
        self.assertFalse((self.view / 'servroot_old').exists())
        self.assertNotIn('servroot_old', os.listdir(self.view))
        path = self.directory('servroot_old')
        (path / 'new').write_text('memory')
        self.assertFalse((path / 'old').exists())
        shutil.rmtree(path)
        self.assertEqual((self.lower / 'servroot_old/old').read_text(), 'preserve')
        self.assertFalse((self.view / 'servroot_old').exists())

    def test_symlinks(self):
        path = self.directory() / 'value'
        path.write_text('target')
        (self.view / 'servroot_link').symlink_to('servroot_a')
        self.assertTrue((self.lower / 'servroot_link').is_symlink())
        self.assertEqual((self.view / 'servroot_link/value').read_text(), 'target')
        self.assertEqual(os.readlink(self.view / 'servroot_link'), 'servroot_a')
        (path.parent / 'link').symlink_to('value')
        self.assertEqual((path.parent / 'link').read_text(), 'target')

    def test_hardlinks_in_memory(self):
        first = self.directory() / 'first'
        first.write_text('shared')
        second = self.directory('cache_second') / 'second'
        os.link(first, second)
        self.assertEqual(first.stat().st_ino, second.stat().st_ino)
        self.assertEqual(first.stat().st_nlink, 2)
        first.unlink()
        self.assertEqual(second.read_text(), 'shared')
        self.assertEqual(second.stat().st_nlink, 1)

    def test_hardlinks_on_source(self):
        first = self.directory('data') / 'first'
        first.write_text('shared')
        second = self.directory('other') / 'second'
        os.link(first, second)
        self.assertEqual(first.stat().st_ino, second.stat().st_ino)
        self.assertEqual((self.lower / 'other/second').read_text(), 'shared')

    def test_cross_backend_links_fail(self):
        mem = self.directory() / 'value'
        mem.write_text('memory')
        disk = self.directory('data') / 'value'
        disk.write_text('disk')
        self.assert_errno(errno.EXDEV, os.link, mem, disk.parent / 'link')
        self.assert_errno(errno.EXDEV, os.link, disk, mem.parent / 'link')
        self.assertFalse((disk.parent / 'link').exists())
        self.assertFalse((mem.parent / 'link').exists())

    def test_rename_within_memory(self):
        first = self.directory()
        (first / 'value').write_text('content')
        first.rename(self.view / 'cache_renamed')
        self.assertFalse(first.exists())
        renamed = self.view / 'cache_renamed'
        self.assertEqual((renamed / 'value').read_text(), 'content')
        (renamed / 'value').rename(renamed / 'second')
        self.assertEqual((self.memory / 'cache_renamed/second').read_text(), 'content')

    def test_rename_on_source(self):
        first = self.directory('data')
        (first / 'value').write_text('content')
        first.rename(self.view / 'other')
        self.assertFalse(first.exists())
        self.assertEqual((self.lower / 'other/value').read_text(), 'content')

    def test_cross_backend_rename_fails(self):
        mem = self.directory()
        disk = self.directory('data')
        (mem / 'a').write_text('a')
        (disk / 'b').write_text('b')
        self.assert_errno(errno.EXDEV, os.rename, mem / 'a', disk / 'a')
        self.assert_errno(errno.EXDEV, os.rename, disk / 'b', mem / 'b')
        self.assert_errno(errno.EXDEV, os.rename, mem, self.view / 'ordinary')
        self.assert_errno(errno.EXDEV, os.rename, disk, self.view / 'cache_new')
        self.assertEqual((mem / 'a').read_text(), 'a')
        self.assertEqual((disk / 'b').read_text(), 'b')

    def test_rename_exchange_and_noreplace(self):
        directory = self.directory()
        first, second = directory / 'a', directory / 'b'
        first.write_text('a')
        second.write_text('b')
        libc = ctypes.CDLL(None, use_errno=True)
        result = libc.renameat2(-100, os.fsencode(first), -100, os.fsencode(second), 1)
        self.assertEqual(result, -1)
        self.assertEqual(ctypes.get_errno(), errno.EEXIST)
        self.assertEqual(libc.renameat2(-100, os.fsencode(first), -100, os.fsencode(second), 2), 0)
        self.assertEqual(first.read_text(), 'b')
        self.assertEqual(second.read_text(), 'a')

    def test_unlink_open_file(self):
        path = self.directory() / 'value'
        path.write_bytes(b'abcdef')
        with path.open('r+b', buffering=0) as file:
            path.unlink()
            self.assertEqual(os.fstat(file.fileno()).st_nlink, 0)
            self.assertEqual(file.read(), b'abcdef')
            file.seek(2)
            file.write(b'XY')
            file.seek(0)
            self.assertEqual(file.read(), b'abXYef')
        self.assertFalse(path.exists())

    def test_truncate_and_seek(self):
        path = self.directory() / 'value'
        with path.open('w+b', buffering=0) as file:
            file.write(b'abcdefgh')
            file.seek(-3, os.SEEK_CUR)
            self.assertEqual(file.read(), b'fgh')
            os.ftruncate(file.fileno(), 4)
            self.assertEqual(os.fstat(file.fileno()).st_size, 4)
            file.seek(0)
            self.assertEqual(file.read(), b'abcd')
        os.truncate(path, 2)
        self.assertEqual(path.read_bytes(), b'ab')
        self.assertEqual((self.memory / 'servroot_a/value').stat().st_size, 2)

    def test_mmap_shared(self):
        path = self.directory() / 'mapped'
        with path.open('w+b') as file:
            file.truncate(8192)
            with mmap.mmap(file.fileno(), 8192) as mapping:
                mapping[4096:4100] = b'mmap'
                mapping.flush()
        with (self.memory / 'servroot_a/mapped').open('rb') as file:
            file.seek(4096)
            self.assertEqual(file.read(4), b'mmap')

    def test_parallel_append(self):
        path = self.directory() / 'append'
        children = []
        for worker in range(4):
            pid = os.fork()
            if pid == 0:
                try:
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                    for index in range(100):
                        record = ('%02d:%04d:' % (worker, index)).encode().ljust(63, b'x') + b'\n'
                        if os.write(fd, record) != 64:
                            os._exit(2)
                    os.close(fd)
                    os._exit(0)
                except BaseException:
                    os._exit(3)
            children.append(pid)
        for pid in children:
            self.assertEqual(os.waitpid(pid, 0)[1], 0)
        lines = path.read_bytes().splitlines()
        self.assertEqual(len(lines), 400)
        self.assertEqual(len(set(lines)), 400)
        self.assertTrue(all(len(line) == 63 for line in lines))

    def test_parallel_metadata(self):
        children = []
        for worker in range(4):
            path = self.directory('cache_worker' + str(worker))
            pid = os.fork()
            if pid == 0:
                try:
                    for index in range(100):
                        first, second = path / 'first', path / 'second'
                        first.write_text(str(index))
                        first.rename(second)
                        if second.read_text() != str(index):
                            os._exit(2)
                        second.unlink()
                    os._exit(0)
                except BaseException:
                    os._exit(3)
            children.append(pid)
        for pid in children:
            self.assertEqual(os.waitpid(pid, 0)[1], 0)

    def test_flock_with_backing_file(self):
        path = self.directory() / 'lock'
        path.touch()
        with path.open('r+') as wrapped, (self.memory / 'servroot_a/lock').open('r+') as native:
            fcntl.flock(native, fcntl.LOCK_EX)
            self.assert_errno(errno.EAGAIN, fcntl.flock, wrapped, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(native, fcntl.LOCK_UN)
            fcntl.flock(wrapped, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(wrapped, fcntl.LOCK_UN)

    def test_posix_lock_close_cleanup(self):
        path = self.directory() / 'lock'
        path.touch()
        with path.open('r+') as wrapped:
            fcntl.lockf(wrapped, fcntl.LOCK_EX)
            duplicate = os.dup(wrapped.fileno())
            os.close(duplicate)
            pid = os.fork()
            if pid == 0:
                try:
                    with (self.memory / 'servroot_a/lock').open('r+') as file:
                        fcntl.lockf(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    os._exit(0)
                except OSError:
                    os._exit(2)
            self.assertEqual(os.waitpid(pid, 0)[1], 0)

    def test_fifo_native_state(self):
        path = self.directory() / 'fifo'
        os.mkfifo(path)
        reader = os.open(self.memory / 'servroot_a/fifo', os.O_RDONLY | os.O_NONBLOCK)
        try:
            writer = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
            try:
                os.write(writer, b'fifo')
                self.assertEqual(os.read(reader, 4), b'fifo')
            finally:
                os.close(writer)
        finally:
            os.close(reader)

    def test_unix_socket(self):
        path = self.directory() / 'server.sock'
        with socket.socket(socket.AF_UNIX) as listener, socket.socket(socket.AF_UNIX) as client:
            listener.bind(str(path))
            listener.listen(1)
            moved = path.with_name('moved.sock')
            path.rename(moved)
            client.connect(str(moved))
            connection, _ = listener.accept()
            with connection:
                client.sendall(b'unix socket')
                self.assertEqual(connection.recv(11), b'unix socket')
        self.assertTrue(stat.S_ISSOCK((self.memory / 'servroot_a/moved.sock').stat().st_mode))

    def test_permissions_and_ownership(self):
        path = self.directory() / 'private'
        path.write_text('private')
        path.chmod(0o600)
        self.assertEqual(self.child_access(path), errno.EACCES)
        path.chmod(0o644)
        self.assertEqual(self.child_access(path), 0)
        os.chown(path, 1001, 1002)
        self.assertEqual((path.stat().st_uid, path.stat().st_gid), (1001, 1002))
        self.assertEqual((self.memory / 'servroot_a/private').stat().st_uid, 1001)

    def test_xattrs(self):
        path = self.directory() / 'attrs'
        path.touch()
        os.setxattr(path, 'user.prefixfs', b'value', os.XATTR_CREATE)
        self.assertEqual(os.getxattr(path, 'user.prefixfs'), b'value')
        self.assertIn('user.prefixfs', os.listxattr(path))
        self.assert_errno(errno.EEXIST, os.setxattr, path, 'user.prefixfs', b'other', os.XATTR_CREATE)
        os.setxattr(path, 'user.prefixfs', b'changed', os.XATTR_REPLACE)
        self.assertEqual(os.getxattr(self.memory / 'servroot_a/attrs', 'user.prefixfs'), b'changed')
        os.removexattr(path, 'user.prefixfs')
        self.assert_errno(errno.ENODATA, os.getxattr, path, 'user.prefixfs')

    def test_acl(self):
        path = self.directory() / 'acl'
        path.write_text('ACL data')
        entries = ((1, 6, 0xffffffff), (2, 4, 1000), (4, 0, 0xffffffff),
                   (16, 4, 0xffffffff), (32, 0, 0xffffffff))
        acl = struct.pack('<I', 2) + b''.join(struct.pack('<HHI', *entry) for entry in entries)
        os.setxattr(path, 'system.posix_acl_access', acl)
        self.assertEqual(os.getxattr(path, 'system.posix_acl_access'), acl)
        self.assertEqual(self.child_access(path), 0)
        path.chmod(0o600)
        self.assertEqual(self.child_access(path), errno.EACCES)
        os.removexattr(path, 'system.posix_acl_access')

    def test_backend_stat(self):
        memory = self.directory() / 'file'
        disk = self.directory('data') / 'file'
        for path, native in ((memory, self.memory / 'servroot_a/file'), (disk, self.lower / 'data/file')):
            path.write_text('value')
            self.assertEqual((path.stat().st_dev, path.stat().st_ino),
                             (native.stat().st_dev, native.stat().st_ino))
            buffer = ctypes.create_string_buffer(256)
            self.assertEqual(ctypes.CDLL(None).statfs(os.fsencode(path), buffer), 0)
            kind = struct.unpack_from('l', buffer)[0]
            self.assertEqual(kind, 0x01021994 if path == memory else 0xef53)

    def test_readdir_and_rewind(self):
        expected = set()
        for index in range(1200):
            name = 'item_%04d' % index
            (self.view / name).touch()
            expected.add(name)
        self.directory('cache_list')
        expected.add('cache_list')
        self.assertEqual(set(os.listdir(self.view)), expected)
        fd = os.open(self.view, os.O_RDONLY | os.O_DIRECTORY)
        try:
            self.assertEqual(set(os.listdir(fd)), expected)
            (self.view / 'new').touch()
            os.lseek(fd, 0, os.SEEK_SET)
            self.assertEqual(set(os.listdir(fd)), expected | {'new'})
        finally:
            os.close(fd)

    def test_data_survives_unmount(self):
        (self.directory() / 'debug.log').write_text('keep for debugging')
        self.unmount()
        self.assertEqual((self.memory / 'servroot_a/debug.log').read_text(), 'keep for debugging')
        self.mount()
        self.assertEqual((self.view / 'servroot_a/debug.log').read_text(), 'keep for debugging')

    def test_busy_unmount(self):
        path = self.directory() / 'open'
        path.touch()
        with path.open('rb'):
            self.assertNotEqual(command('umount', str(self.view), check=False).returncode, 0)
        self.unmount()
        self.mount()

    def test_read_only(self):
        path = self.directory() / 'file'
        path.write_text('read only')
        self.unmount()
        self.mount(',ro')
        self.assertEqual(path.read_text(), 'read only')
        self.assert_errno(errno.EROFS, path.write_text, 'changed')
        self.assert_errno(errno.EROFS, os.mkdir, self.view / 'cache_new')
        self.assertEqual((self.memory / 'servroot_a/file').read_text(), 'read only')

    def test_invalid_mounts(self):
        self.unmount()
        for option in ('memory=' + str(self.memory),
                       'memory=' + str(self.memory) + ',prefix=',
                       'memory=' + str(self.memory) + ',prefix=.',
                       'memory=' + str(self.memory) + ',prefix=..',
                       'memory=' + str(self.memory) + ',prefix=a/b',
                       'memory=' + str(self.lower) + ',prefix=cache_'):
            with self.subTest(option=option):
                result = command('mount', '-t', 'prefixfs', str(self.lower), str(self.view), '-o', option, check=False)
                self.assertNotEqual(result.returncode, 0)
        self.mount()

    def test_privileged_execution_not_exposed(self):
        self.unmount()
        program = self.lower / 'uid-program'
        shutil.copyfile('/bin/pf-uid', program)
        program.chmod(0o4755)
        self.mount()
        wrapped = self.view / 'uid-program'
        self.assertFalse(wrapped.stat().st_mode & stat.S_ISUID)
        pid = os.fork()
        if pid == 0:
            os.setgroups([])
            os.setgid(1000)
            os.setuid(1000)
            os.execl(str(wrapped), str(wrapped))
        self.assertEqual(os.waitpid(pid, 0)[1], 0)
        self.assert_errno(errno.EOPNOTSUPP, os.chmod, wrapped, 0o4755)

    def test_regular_executable_in_memory(self):
        program = self.directory('build_work') / 'program'
        shutil.copyfile('/bin/pf-uid', program)
        program.chmod(0o755)
        self.assertEqual(command(str(program), check=False).returncode, 42)

    def test_noexec_backing_mount(self):
        self.unmount()
        command('mount', '--bind', str(self.memory), str(self.memory))
        try:
            command('mount', '-o', 'remount,bind,noexec', str(self.memory))
            self.mount()
            program = self.directory() / 'program'
            shutil.copyfile('/bin/pf-uid', program)
            program.chmod(0o755)
            self.assert_errno(errno.EACCES, command, str(program))
            with program.open('rb') as file:
                self.assert_errno(errno.EPERM, mmap.mmap, file.fileno(), 4096,
                                  prot=mmap.PROT_READ | mmap.PROT_EXEC)
                with mmap.mmap(file.fileno(), 4096, prot=mmap.PROT_READ | mmap.PROT_WRITE,
                               flags=mmap.MAP_PRIVATE) as mapping:
                    address = ctypes.addressof(ctypes.c_char.from_buffer(mapping))
                    libc = ctypes.CDLL(None, use_errno=True)
                    result = libc.mprotect(ctypes.c_void_p(address), 4096,
                                           mmap.PROT_READ | mmap.PROT_EXEC)
                    self.assertEqual(result, -1)
                    self.assertEqual(ctypes.get_errno(), errno.EACCES)
        finally:
            if self.mounted:
                self.unmount()
            command('umount', str(self.memory))
            self.mount()

    def test_file_preallocation_and_hole_punch(self):
        libc = ctypes.CDLL(None, use_errno=True)
        for name in ('servroot_a', 'data'):
            path = self.directory(name) / 'preallocated'
            with path.open('w+b', buffering=0) as file:
                os.posix_fallocate(file.fileno(), 0, 16384)
                self.assertEqual(os.fstat(file.fileno()).st_size, 16384)
                file.write(b'x' * 16384)
                self.assertEqual(libc.fallocate(file.fileno(), 3,
                                               ctypes.c_longlong(4096),
                                               ctypes.c_longlong(4096)), 0)
                file.seek(0)
                self.assertEqual(file.read(), b'x' * 4096 + b'\0' * 4096 + b'x' * 8192)
                os.fsync(file.fileno())

    def test_sendfile_and_copy_file_range(self):
        data = bytes(range(256)) * 256
        for name in ('servroot_a', 'data'):
            directory = self.directory(name)
            first, second, third = (directory / name for name in ('a', 'b', 'c'))
            first.write_bytes(data)
            with first.open('rb') as source, second.open('wb') as destination:
                self.assertEqual(os.sendfile(destination.fileno(), source.fileno(), 0, len(data)), len(data))
            self.assertEqual(second.read_bytes(), data)
            with first.open('rb') as source, third.open('wb') as destination:
                self.assertEqual(os.copy_file_range(source.fileno(), destination.fileno(), len(data)), len(data))
            self.assertEqual(third.read_bytes(), data)

    def test_directory_cookies(self):
        for directory in (self.view, self.directory()):
            names = {'entry_%03d' % index for index in range(70)}
            for name in names:
                (directory / name).touch()
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                def entries():
                    buffer = ctypes.create_string_buffer(80)
                    libc = ctypes.CDLL(None, use_errno=True)
                    count = libc.syscall(217, fd, buffer, len(buffer))  # x86_64 getdents64
                    self.assertGreaterEqual(count, 0)
                    result = []
                    offset = 0
                    while offset < count:
                        cookie, length = struct.unpack_from('qH', buffer.raw, offset + 8)
                        name = buffer.raw[offset + 19:offset + length].split(b'\0', 1)[0].decode()
                        result.append((cookie, name))
                        offset += length
                    return result
                first = entries()
                self.assertTrue(first)
                cookie = first[-1][0]
                second = entries()
                os.lseek(fd, cookie, os.SEEK_SET)
                self.assertEqual(entries(), second)
                os.lseek(fd, 0, os.SEEK_SET)
                found = []
                while True:
                    batch = entries()
                    if not batch:
                        break
                    found.extend(name for _, name in batch)
                expected = names | {'.', '..'}
                if directory == self.view:
                    expected.add('servroot_a')
                self.assertEqual(set(found), expected)
                self.assertEqual(len(found), len(expected))
            finally:
                os.close(fd)

    def test_rename_replacement_and_exchange_directories(self):
        first = self.directory('cache_first')
        second = self.directory('cache_second')
        (first / 'value').write_text('first')
        first.rename(second)
        self.assertFalse(first.exists())
        self.assertEqual((second / 'value').read_text(), 'first')
        first.mkdir()
        (first / 'value').write_text('new first')
        libc = ctypes.CDLL(None, use_errno=True)
        self.assertEqual(libc.renameat2(-100, os.fsencode(first), -100, os.fsencode(second), 2), 0)
        self.assertEqual((first / 'value').read_text(), 'first')
        self.assertEqual((second / 'value').read_text(), 'new first')
        a, b = first / 'a', first / 'b'
        a.write_text('a')
        b.write_text('b')
        with b.open('rb') as previous:
            a.rename(b)
            self.assertEqual(previous.read(), b'b')
            self.assertEqual(b.read_text(), 'a')
            self.assertEqual(os.fstat(previous.fileno()).st_nlink, 0)

    def test_exchange_cannot_move_source_directory_to_matching_name(self):
        first = self.view / 'cache_file'
        first.write_text('source file')
        second = self.directory('data')
        (second / 'value').write_text('source directory')
        libc = ctypes.CDLL(None, use_errno=True)
        self.assertEqual(libc.renameat2(-100, os.fsencode(first), -100, os.fsencode(second), 2), -1)
        self.assertEqual(ctypes.get_errno(), errno.EXDEV)
        self.assertEqual(first.read_text(), 'source file')
        self.assertEqual((second / 'value').read_text(), 'source directory')
        self.unmount()
        self.mount()
        self.assertEqual(first.read_text(), 'source file')
        self.assertEqual((second / 'value').read_text(), 'source directory')

    def test_mount_over_source(self):
        self.unmount()
        (self.lower / 'existing').write_text('on disk')
        options = 'memory=' + str(self.memory) + ',prefix=servroot'
        command('mount', '-t', 'prefixfs', str(self.lower), str(self.lower), '-o', options)
        try:
            self.assertEqual((self.lower / 'existing').read_text(), 'on disk')
            (self.lower / 'servroot_new').mkdir()
            (self.lower / 'servroot_new/log').write_text('in memory')
            (self.lower / 'ordinary').write_text('disk write')
            self.assertEqual((self.memory / 'servroot_new/log').read_text(), 'in memory')
        finally:
            command('umount', str(self.lower))
        self.assertFalse((self.lower / 'servroot_new').exists())
        self.assertEqual((self.lower / 'ordinary').read_text(), 'disk write')
        self.mount()

    def test_posix_lock_contention(self):
        path = self.directory() / 'lock'
        path.touch()
        with path.open('r+') as wrapped:
            fcntl.lockf(wrapped, fcntl.LOCK_EX)
            pid = os.fork()
            if pid == 0:
                try:
                    with (self.memory / 'servroot_a/lock').open('r+') as native:
                        fcntl.lockf(native, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as error:
                    os._exit(0 if error.errno in (errno.EAGAIN, errno.EACCES) else 2)
                os._exit(3)
            self.assertEqual(os.waitpid(pid, 0)[1], 0)

    def test_capabilities_not_exposed(self):
        path = self.directory() / 'program'
        path.touch()
        capability = struct.pack('<IIIII', 0x02000001, 1, 0, 0, 0)
        self.assert_errno(errno.ENODATA, os.getxattr, path, 'security.capability')
        self.assert_errno(errno.EOPNOTSUPP, os.setxattr, path, 'security.capability', capability)

    def test_backing_submount_rejected(self):
        path = self.directory('data')
        nested = self.lower / 'data/nested'
        nested.mkdir()
        command('mount', '-t', 'tmpfs', 'tmpfs', str(nested))
        try:
            self.assert_errno(errno.EXDEV, os.stat, path / 'nested')
        finally:
            command('umount', str(nested))



if __name__ == '__main__':
    if not Path('/prefixfs-vm-marker').is_file():
        raise SystemExit('Run via tools/vm-test.py; host execution is disabled.')
    print('Guest kernel:', os.uname().release, flush=True)
    unittest.main(verbosity=2)
