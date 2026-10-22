#!/usr/bin/env python3
"""Run a command with selected directory-name prefixes backed by tmpfs."""

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parent
PROJECT_URL = "https://github.com/zhuizhuhaomeng/prefixfs"


def infrastructure_env():
    env = os.environ.copy()
    for name in ("LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME"):
        env.pop(name, None)
    return env


def privileged(command):
    return command if os.geteuid() == 0 else ["sudo", "-n", "--", *command]


def validate_prefixes(values):
    prefixes = list(dict.fromkeys(values))
    if not prefixes or len(prefixes) > 64 or any(
            not prefix or prefix in (".", "..") or "/" in prefix or "\0" in prefix
            or "," in prefix or len(os.fsencode(prefix)) > 255 for prefix in prefixes):
        raise RuntimeError("specify 1 to 64 non-empty directory-name prefixes with "
                           "--prefix or PREFIXFS_PREFIXES")
    return prefixes


def prefixes_from_env():
    return validate_prefixes([part.strip() for part in
                              os.environ.get("PREFIXFS_PREFIXES", "").split(",")])


def module_loaded():
    return Path("/sys/module/prefixfs").is_dir()


def prepare_module():
    for tool in ("unshare", "mount", "bwrap"):
        if shutil.which(tool) is None:
            raise RuntimeError("requires " + tool)
    if os.geteuid() != 0 and shutil.which("sudo") is None:
        raise RuntimeError("mounting prefixfs requires root or passwordless sudo")
    loaded = module_loaded()
    if loaded and not (os.environ.get("PREFIXFS_KO") or os.environ.get("PREFIXFS_HOME")):
        return

    home = Path(os.environ.get("PREFIXFS_HOME") or ROOT)
    module = Path(os.environ.get("PREFIXFS_KO") or home / "prefixfs.ko").resolve()
    if not module.is_file():
        raise RuntimeError("module not found at " + str(module) + "; build "
                           + PROJECT_URL + " for the running kernel; set PREFIXFS_HOME or PREFIXFS_KO")
    env = infrastructure_env()
    release = subprocess.check_output(["modinfo", "-F", "vermagic", str(module)],
                                      env=env, text=True).split()[0]
    if release != os.uname().release:
        raise RuntimeError("module kernel " + release + " does not match "
                           + os.uname().release + "; rebuild " + str(module))
    if loaded:
        return
    result = subprocess.run(privileged(["insmod", str(module)]), env=env)
    # Another test checkout may have loaded the module concurrently.
    if result.returncode and not module_loaded():
        raise RuntimeError("cannot load " + str(module) + "; root or passwordless sudo is required")


def namespace_main():
    """Mount as root, then restore the caller's identity before running the command."""
    if os.geteuid() != 0:
        raise RuntimeError("namespace setup requires root")
    # Send environment over a pipe, rather than exposing it in argv or files.
    config = json.load(sys.stdin)
    options = "memory=" + config["memory"] + "," + ",".join(
        "prefix=" + prefix for prefix in config["prefixes"])
    subprocess.run(["mount", "-i", "-t", "prefixfs", "-o", options,
                    config["source"], config["source"]],
                   env=infrastructure_env(), check=True)
    os.setgroups(config["groups"])
    os.setgid(config["gid"])
    os.setuid(config["uid"])
    env = config["env"]
    preload = env.pop("LD_PRELOAD", "")
    libraries = env.pop("LD_LIBRARY_PATH", "")
    # Bubblewrap stops child processes left behind by the command. Apply
    # preloaded libraries only after infrastructure setup.
    command = [config["bwrap"], "--die-with-parent", "--unshare-pid",
               "--bind", "/", "/", "--dev-bind", "/dev", "/dev", "--proc", "/proc",
               "--chdir", config["cwd"], "--setenv", "LD_PRELOAD", preload,
               "--setenv", "LD_LIBRARY_PATH", libraries, "--", *config["command"]]
    os.execve(config["bwrap"], command, env)


def stop(process):
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except ProcessLookupError:
        process.wait()
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def run(source, parent, command, preload="", prefixes=None, cleanup=False):
    prefixes = validate_prefixes(prefixes) if prefixes is not None else prefixes_from_env()
    source = source.resolve(strict=True)
    parent = parent.resolve(strict=True)
    if not source.is_dir() or not parent.is_dir():
        raise RuntimeError("source and tmpfs parent must be directories")
    if source == parent or source in parent.parents:
        raise RuntimeError("tmpfs parent must be outside the source directory")
    if "," in str(parent):
        raise RuntimeError("tmpfs parent must not contain a comma")
    kind = subprocess.check_output(["stat", "-f", "-c", "%T", str(parent)],
                                   env=infrastructure_env(), text=True).strip()
    if kind != "tmpfs":
        raise RuntimeError("backing parent must be on tmpfs: " + str(parent))
    prepare_module()

    base = Path(tempfile.mkdtemp(prefix="prefixfs-", dir=parent))
    child = None
    status = None
    try:
        memory = base / "memory"
        memory.mkdir()
        env = os.environ.copy()
        env["LD_PRELOAD"] = preload
        config = dict(source=str(source), memory=str(memory), prefixes=prefixes,
                      uid=os.getuid(), gid=os.getgid(), groups=os.getgroups(),
                      cwd=str(Path.cwd()), env=env, bwrap=shutil.which("bwrap"), command=command)
        patterns = ", ".join(prefix + "*" for prefix in prefixes)
        print("prefixfs: " + str(source) + "/ [" + patterns + "] -> " + str(memory), flush=True)
        launcher = privileged(["unshare", "--mount", "--fork", "--kill-child",
                               "--propagation", "private", sys.executable, "-I",
                               str(Path(__file__).resolve()), "--namespace"])
        child = subprocess.Popen(launcher, stdin=subprocess.PIPE,
                                 env=infrastructure_env(), start_new_session=True)
        payload = json.dumps(config).encode()
        child.communicate(payload)
        status = child.returncode
        return status if status >= 0 else 128 - status
    finally:
        stop(child)
        # Never recurse into the mounted source. Only this newly allocated
        # backing tree is eligible for cleanup after the namespace has exited.
        if status == 0 and cleanup:
            shutil.rmtree(base)
        else:
            print("prefixfs: retained diagnostics at " + str(base), file=sys.stderr, flush=True)


def main():
    if sys.argv[1:] == ["--namespace"]:
        try:
            namespace_main()
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            print("prefixfs: namespace setup: " + str(error), file=sys.stderr)
            return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path.cwd(),
                        help="directory to route (default: current working directory)")
    parser.add_argument("--prefix", action="append", help="directory-name prefix; repeat for multiple prefixes")
    parser.add_argument("--cleanup", action="store_true",
                        help="remove backing data after success; failures always retain it")
    parser.add_argument("--tmpdir", type=Path, default=Path("/tmp"))
    parser.add_argument("--preload", default=os.environ.get("LD_PRELOAD", ""))
    parser.add_argument("--check", action="store_true", help="check dependencies and load the kernel module")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not args.check and not command:
        parser.error("a command is required after --")

    def interrupted(signum, _frame):
        for handled in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(handled, signal.SIG_IGN)
        raise InterruptedError(signum)

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, interrupted)
    try:
        if args.check:
            if args.prefix is not None:
                validate_prefixes(args.prefix)
            elif "PREFIXFS_PREFIXES" in os.environ:
                prefixes_from_env()
            prepare_module()
            return 0
        return run(args.source, args.tmpdir, command, args.preload,
                   prefixes=args.prefix, cleanup=args.cleanup)
    except InterruptedError as error:
        return 128 + error.args[0]
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print("prefixfs: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
