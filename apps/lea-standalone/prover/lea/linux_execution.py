"""Linux command confinement using Landlock (ABI >= 3) and seccomp.

Runs in a fresh, single-threaded child before exec, never in a worker thread.
The filesystem rules and network filter are inherited by every descendant.
Initialization failures exit without executing the requested command.
"""
from __future__ import annotations

import ctypes
import errno
import json
import os
from pathlib import Path
import platform
import sys


class PathBeneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def checked(result: int, operation: str) -> int:
    if result < 0:
        code = ctypes.get_errno()
        raise OSError(code, f"{operation}: {os.strerror(code)}")
    return result


def restrict(policy: dict) -> None:
    if platform.machine() not in {"x86_64", "aarch64", "arm64"}:
        raise RuntimeError("Linux confinement supports only AMD64 and ARM64")
    roots = {str(Path(p).resolve(strict=True)) for p in policy["writable_roots"]}
    if not roots or "/" in roots or any(not Path(p).is_dir() for p in roots):
        raise ValueError("An executor requires bounded existing writable directories")
    files = {str(Path(p).resolve(strict=True)) for p in policy.get("writable_files", [])}
    # macOS can authorize metadata on a directory itself with a literal rule;
    # Landlock cannot do that without widening access to its whole subtree.
    # Keep directory metadata read-only on Linux.
    files = {p for p in files if Path(p).is_file()}

    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    abi = checked(libc.syscall(444, None, 0, 1), "Landlock ABI query")
    if abi < 3:
        raise RuntimeError("Landlock ABI 3 or newer is required for truncate protection")
    # WRITE_FILE, REMOVE_*, MAKE_*, REFER, TRUNCATE; handle device ioctls too
    # where supported. Reading and executing installed tools remain allowed.
    handled = (1 << 1) | sum(1 << bit for bit in range(4, 15))
    if abi >= 5:
        handled |= 1 << 15
    attr = ctypes.c_uint64(handled)
    ruleset = checked(libc.syscall(444, ctypes.byref(attr), ctypes.sizeof(attr), 0), "Landlock ruleset")
    try:
        for name in sorted(roots | files | {"/dev/null"}):
            fd = os.open(name, os.O_PATH | os.O_CLOEXEC)
            try:
                rights = handled if name in roots else handled & ((1 << 1) | (1 << 14) | (1 << 15))
                rule = PathBeneath(rights, fd)
                checked(libc.syscall(445, ruleset, 1, ctypes.byref(rule), 0), "Landlock path rule")
            finally:
                os.close(fd)
        checked(libc.prctl(38, 1, 0, 0, 0), "no_new_privs")
        checked(libc.syscall(446, ruleset, 0), "Landlock restriction")
    finally:
        os.close(ruleset)

    # Landlock's TCP rules alone do not cover Unix sockets, UDP or io_uring.
    # libseccomp resolves native syscall numbers and rejects other syscall ABIs.
    seccomp = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    context = seccomp.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not context:
        raise RuntimeError("Unable to create seccomp filter")
    try:
        for name in ("socket", "socketpair", "connect", "bind", "listen", "accept", "accept4",
                     "sendto", "sendmsg", "sendmmsg", "io_uring_setup", "io_uring_enter", "io_uring_register",
                     "ptrace", "process_vm_writev"):
            number = seccomp.seccomp_syscall_resolve_name(name.encode())
            if number < 0:
                raise RuntimeError(f"libseccomp does not recognize required syscall {name}")
            result = seccomp.seccomp_rule_add(context, 0x00050000 | errno.EPERM, number, 0)
            if result < 0:
                raise OSError(-result, f"Unable to restrict {name}")
        result = seccomp.seccomp_load(context)
        if result < 0:
            raise OSError(-result, "Unable to install seccomp filter")
    finally:
        seccomp.seccomp_release(context)


def main() -> int:
    try:
        policy = json.loads(sys.argv[1])
        command = sys.argv[2:]
        if not command:
            raise ValueError("Missing confined command")
        restrict(policy)
        os.execvpe(command[0], command, os.environ)
    except Exception as error:
        print(f"Linux confinement failed: {error}", file=sys.stderr)
        return 126


if __name__ == "__main__":
    raise SystemExit(main())
