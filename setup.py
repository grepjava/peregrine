"""Builds the Swift server and packages it.

By default the server is built as ``peregrine._native``, a CPython extension
module. The interpreter that imports it supplies Python -- no libpython is
linked -- and on a distribution python, which is a statically linked,
position-dependent executable, framework code runs 10-15 % faster that way than
in the shared libpython an embedding executable has to use (BENCHMARKS.md).

PEREGRINE_BUILD=binary builds the standalone executable instead, which embeds
libpython and is started by the launcher with execv. Both take the same command
line, and ``peregrine`` runs whichever the package carries.

Either way the result is bound to one CPython ABI, so a wheel is tagged for
exactly the interpreter and platform it was built for
(`cp312-cp312-manylinux_2_39_x86_64`, `cp314-cp314t-...`) and pip refuses it
anywhere it would not load. When no wheel matches, pip falls back to the sdist
and this file compiles the server for the installing interpreter.

The wheel does not vendor libpython -- that would fight the interpreter the
user already has. It does vendor the Swift runtime, with a relative rpath, so a
machine that has never seen this toolchain can still load the server.

Requirements when compiling (sdist or `scripts/build-wheel.sh`):
  * a Swift toolchain (swift 6.1 or newer) on PATH
  * the Python development files for the interpreter being installed into,
    found by pkg-config as python3 (python3-embed for the binary)
  * patchelf on Linux, so the Swift runtime can travel with the server
"""

import glob
import os
import re
import shutil
import subprocess
import sys
import sysconfig

from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.dist import Distribution

try:                                    # setuptools >= 70.1 vendors its own
    from setuptools.command.bdist_wheel import bdist_wheel
except ImportError:                     # older setuptools defers to `wheel`
    try:
        from wheel.bdist_wheel import bdist_wheel
    except ImportError:
        bdist_wheel = None

HERE = os.path.dirname(os.path.abspath(__file__))
BINARY = "peregrine"
EXTENSION_PRODUCT = "PeregrineExtension"


def _fail(message):
    sys.stderr.write("\nperegrine: %s\n\n" % message)
    raise SystemExit(1)


def _mode():
    """"extension" (the default) or "binary", from PEREGRINE_BUILD."""
    mode = os.environ.get("PEREGRINE_BUILD", "").strip().lower() or "extension"
    if mode not in ("extension", "binary"):
        _fail("PEREGRINE_BUILD must be extension or binary, not %r" % mode)
    return mode


def _free_threaded():
    """True when this interpreter is a free-threaded build (PEP 703)."""
    return bool(sysconfig.get_config_var("Py_GIL_DISABLED"))


def _running_version():
    """The interpreter this build must produce a server for.

    A free-threaded build is "3.14t", not "3.14". The suffix is not cosmetic:
    it is a different ABI, so a server built for one cannot load in the other,
    and a wheel that claimed otherwise would install happily and then fail.
    """
    return "%d.%d%s" % (sys.version_info[0], sys.version_info[1],
                        "t" if _free_threaded() else "")


def _same_release(pkg_version, running):
    """Whether two version strings name the same CPython release.

    Major and minor and nothing else. The free-threaded suffix is deliberately
    ignored here: it describes an ABI rather than a release, and pkg-config
    does not report it even when the headers it found are the free-threaded
    ones.
    """
    def numbers(text):
        match = re.match(r"(\d+)\.(\d+)", text)
        return match.groups() if match else None

    left, right = numbers(pkg_version), numbers(running)
    return left is not None and left == right


def _pkg_config_name(mode, env):
    """The pkg-config package for this interpreter.

    python3-embed adds -lpython3.x, which the executable needs and the
    extension must not have: a second libpython loaded into a python that
    already contains one is two interpreters' worth of globals.

    The versioned name is preferred. python3.pc is only an alias, and an
    installation is not obliged to carry it: a versioned Homebrew keg ships
    python-3.13.pc alone, and pkg-config then answers `python3` with whichever
    other Python on the search path has one.
    """
    suffix = "" if mode == "extension" else "-embed"
    ldversion = sysconfig.get_config_var("LDVERSION")
    if ldversion and shutil.which("pkg-config"):
        versioned = "python-%s%s" % (ldversion, suffix)
        probe = subprocess.run(["pkg-config", "--exists", versioned], env=env)
        if probe.returncode == 0:
            return versioned
    return "python3" + suffix


def _windows():
    return sys.platform == "win32"


def _openssl_dir():
    """Windows: the OpenSSL installation aviancore links against, or None.

    OPENSSL_DIR names it; otherwise the location the Shining Light installer
    uses. It needs include\\ and the MD import libraries under lib\\VC\\x64\\MD,
    and its DLLs are vendored into the wheel next to the Swift runtime's.
    """
    for candidate in (os.environ.get("OPENSSL_DIR"),
                      os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                                   "OpenSSL-Win64")):
        if candidate and os.path.isfile(os.path.join(candidate, "include", "openssl", "ssl.h")):
            return candidate
    return None


def _msvc_environment(env):
    """Windows: the variables vcvars64.bat sets, merged into `env`.

    The Swift toolchain links with the MSVC tools and libraries, and clang
    finds the Windows SDK and OpenSSL through INCLUDE and LIB. A build started
    from a developer prompt has them already.
    """
    if env.get("VCINSTALLDIR"):
        return env
    vswhere = os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                           "Microsoft Visual Studio", "Installer", "vswhere.exe")
    if not os.path.isfile(vswhere):
        _fail("Visual Studio (or its Build Tools) is required to build on Windows:\n"
              "Swift links with the MSVC toolchain. vswhere.exe was not found.")
    install = subprocess.run([vswhere, "-latest", "-products", "*", "-property",
                              "installationPath"], capture_output=True, text=True).stdout.strip()
    vcvars = os.path.join(install, "VC", "Auxiliary", "Build", "vcvars64.bat")
    if not os.path.isfile(vcvars):
        _fail("vcvars64.bat was not found under %s; install the C++ build tools." % install)
    dump = subprocess.run('"%s" >nul && set' % vcvars, shell=True, capture_output=True,
                          text=True, env=env).stdout
    for line in dump.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            env[key] = value
    return env


def _build_environment(mode):
    """The environment the Swift build runs in.

    pkg-config is pointed at this interpreter's own .pc files after anything
    the user set, so the headers found belong to the python running pip rather
    than to whichever one the system search path lists first. Relocated builds
    (uv's, for one) report the directory they were built in for LIBPC, which
    does not exist here, so LIBDIR/pkgconfig is tried as well.
    """
    env = os.environ.copy()
    if mode == "extension":
        env["PEREGRINE_EXTENSION"] = "1"
    else:
        env.pop("PEREGRINE_EXTENSION", None)
    if _windows():
        # No pkg-config: Package.swift takes the installation directly, which
        # holds include\ and the import libraries in libs\.
        env["PEREGRINE_PYTHON_HOME"] = sys.base_prefix
        env["PEREGRINE_PYTHON_FT"] = "1" if _free_threaded() else "0"
        env = _msvc_environment(env)
        openssl = _openssl_dir()
        if openssl:
            env["INCLUDE"] = os.pathsep.join([os.path.join(openssl, "include"),
                                              env.get("INCLUDE", "")])
            env["LIB"] = os.pathsep.join([os.path.join(openssl, "lib", "VC", "x64", "MD"),
                                          env.get("LIB", "")])
            env["PATH"] = os.pathsep.join([os.path.join(openssl, "bin"), env.get("PATH", "")])
        return env
    candidates = [sysconfig.get_config_var("LIBPC"),
                  os.path.join(sysconfig.get_config_var("LIBDIR") or "", "pkgconfig")]
    found = [path for path in candidates if path and os.path.isdir(path)]
    if found:
        existing = env.get("PKG_CONFIG_PATH")
        env["PKG_CONFIG_PATH"] = os.pathsep.join(([existing] if existing else []) + found[:1])
    # Package.swift reads the package name from here.
    env["PEREGRINE_PYTHON_PC"] = _pkg_config_name(mode, env)
    return env


def _headers_free_threaded(package, env):
    """Whether the headers pkg-config found define Py_GIL_DISABLED, or None.

    Every include directory is read, because Debian's pyconfig.h is a wrapper
    that includes the real one from an architecture directory.
    """
    result = subprocess.run(["pkg-config", "--cflags-only-I", package],
                            capture_output=True, text=True, env=env)
    seen = False
    for flag in result.stdout.split():
        if not flag.startswith("-I"):
            continue
        path = os.path.join(flag[2:], "pyconfig.h")
        if not os.path.isfile(path):
            continue
        seen = True
        with open(path) as fh:
            if re.search(r"^\s*#\s*define\s+Py_GIL_DISABLED\s+1", fh.read(), re.M):
                return True
    return False if seen else None


def _check_toolchain(mode, env):
    if shutil.which("swift", path=env.get("PATH")) is None:
        _fail(
            "no Swift toolchain found on PATH.\n"
            "Peregrine is compiled at install time; install Swift 6.1 or newer\n"
            "from https://swift.org/install and try again."
        )
    if _windows():
        home = env["PEREGRINE_PYTHON_HOME"]
        if not os.path.isfile(os.path.join(home, "include", "Python.h")) \
                or not os.path.isdir(os.path.join(home, "libs")):
            _fail("this Python installation (%s) has no include\\Python.h or no libs\\\n"
                  "directory, which building the extension needs." % home)
        if _openssl_dir() is None:
            _fail("OpenSSL was not found. Install OpenSSL for Windows (x64, not Light)\n"
                  "or set OPENSSL_DIR to an installation with include\\ and lib\\VC\\x64\\MD.")
        return
    package = env["PEREGRINE_PYTHON_PC"]
    if shutil.which("pkg-config") is None:
        _fail("pkg-config is required to locate the Python development files.")
    probe = subprocess.run(["pkg-config", "--exists", package], env=env)
    if probe.returncode != 0:
        _fail(
            "pkg-config cannot find %s.\n"
            "Install the Python development files for this interpreter\n"
            "(python3-dev on Debian and Ubuntu, python3-devel on Fedora,\n"
            "or a python.org / Homebrew framework build on macOS), or point\n"
            "PKG_CONFIG_PATH at its lib/pkgconfig." % package
        )
    # pkg-config decides which headers Swift compiles against, and it is not
    # obliged to point at the interpreter running this build. Catching that
    # here gives a clear message instead of a wheel that fails at import time.
    version = subprocess.run(["pkg-config", "--modversion", package],
                             capture_output=True, text=True, env=env)
    resolved = version.stdout.strip()
    # Only the numbers are comparable here. A free-threaded interpreter calls
    # itself "3.14t", but its own .pc file says "3.14", exactly as the GIL
    # build's does. The ABI is settled separately: for the extension from the
    # headers below, for the binary by asking the binary once it is built.
    if resolved and not _same_release(resolved, _running_version()):
        _fail(
            "pkg-config resolves %s to Python %s, but this build is\n"
            "running under Python %s. The server would be built for the wrong\n"
            "interpreter. Install the development files for %s, or run pip\n"
            "from the interpreter you intend to serve with."
            % (package, resolved, _running_version(), _running_version())
        )
    if mode == "extension":
        headers = _headers_free_threaded(package, env)
        if headers is not None and headers != _free_threaded():
            _fail(
                "pkg-config found %s headers for Python %s, but this build is\n"
                "running under %s Python %s. An extension compiled against one\n"
                "does not load in the other. Point PKG_CONFIG_PATH at the\n"
                "lib/pkgconfig of the interpreter running pip."
                % ("free-threaded" if headers else "GIL", resolved,
                   "free-threaded" if _free_threaded() else "GIL", _running_version())
            )


def _linked_version(binary):
    """Asks the built binary which libpython it actually bound.

    The authority is the binary, not pkg-config and not the headers: it reports
    Py_GetVersion() from the library the loader resolved.
    """
    env = os.environ.copy()
    libdir = sysconfig.get_config_var("LIBDIR")
    if libdir:
        key = "DYLD_LIBRARY_PATH" if sys.platform == "darwin" else "LD_LIBRARY_PATH"
        env[key] = libdir + os.pathsep + env.get(key, "")
    result = subprocess.run(
        [binary, "--version"], capture_output=True, text=True, env=env)
    match = re.search(r"CPython (\d+)\.(\d+)([^)]*)", result.stdout)
    if not match:
        return None
    suffix = "t" if "free-threaded" in match.group(3) else ""
    return "%s.%s%s" % (match.group(1), match.group(2), suffix)


def _check_import(build_lib):
    """Imports the built extension in a fresh interpreter, as a user would.

    The loader search path is cleared first, so a Swift runtime found only
    through the environment -- rather than through the vendored copy -- fails
    here instead of on somebody else's machine. On a free-threaded interpreter
    the import must also leave the GIL off: an extension that does not declare
    itself safe switches it back on for the whole process.
    """
    probe = (
        "import sys\n"
        "import peregrine._native as native\n"
        "gil = getattr(sys, '_is_gil_enabled', lambda: True)()\n"
        "print(native.__file__)\n"
        "print('gil' if gil else 'nogil')\n"
    )
    env = os.environ.copy()
    for key in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "PYTHONPATH"):
        env.pop(key, None)
    if _windows():
        # Windows finds DLLs on PATH, so PATH loses everything but the system
        # and this Python: the Swift runtime and OpenSSL have to come from the
        # copies vendored into the package.
        root = os.environ.get("SystemRoot", r"C:\Windows")
        env["PATH"] = os.pathsep.join([os.path.join(root, "System32"), root,
                                       sys.base_prefix])
    result = subprocess.run([sys.executable, "-c", probe], cwd=build_lib,
                            capture_output=True, text=True, env=env)
    if result.returncode != 0:
        _fail("the built extension does not import:\n%s" % result.stderr.strip())
    if _free_threaded() and "nogil" not in result.stdout.split():
        _fail("importing peregrine._native turned the GIL back on; the module\n"
              "must declare Py_MOD_GIL_NOT_USED.")
    return result.stdout.split()[0]


def _manylinux_platform_tag(platform_tag):
    """PyPI rejects the linux_* tag. It accepts manylinux_x_y, which is a
    glibc floor. That is what this build can honestly claim: the server
    was linked on this glibc, so it will not load on an older one.

    It is not a full auditwheel repair. libpython, libssl and libicu stay
    with the machine -- the first because the wheel tag already promised
    one interpreter, the others because they are ordinary system libraries
    on the distros this tag will install onto.
    """
    if not platform_tag.startswith("linux_"):
        return platform_tag
    try:
        raw = os.confstr("CS_GNU_LIBC_VERSION") or ""
    except (ValueError, OSError):
        raw = ""
    match = re.match(r"glibc (\d+)\.(\d+)", raw)
    if not match:
        _fail(
            "this Linux build has no glibc version, so it cannot be tagged\n"
            "for PyPI. manylinux_x_y is a glibc floor; without one there is\n"
            "nothing honest to write."
        )
    arch = platform_tag[len("linux_"):]
    return "manylinux_%s_%s_%s" % (match.group(1), match.group(2), arch)


# The Swift runtime travels with the server. libpython, libssl and the rest of
# the OS do not -- they belong to the machine that runs the wheel.
_BUNDLE_HINTS = ("swift", "dispatch", "blocksruntime")
_NEVER_BUNDLE = (
    "libpython", "libssl", "libcrypto", "libc.so", "libm.so", "libdl.so",
    "libpthread", "librt.so", "libgcc", "libstdc++", "ld-linux",
    "linux-vdso", "libz.so", "libxml2", "libicu", "libcurl", "libbsd",
    "libedit", "libncurses", "libsqlite", "liblzma", "libffi", "libsystem",
)


def _require_relocate():
    return os.environ.get("PEREGRINE_REQUIRE_RELOCATE", "").strip() not in ("", "0")


def _should_bundle(name, path):
    # Match against the SONAME / basename, from the start. A substring
    # check on the whole line would skip libswiftGlibc.so because
    # "libc.so" sits inside "glibc.so".
    base = os.path.basename(name or path).lower()
    if any(base == token or base.startswith(token) for token in _NEVER_BUNDLE):
        return False
    haystack = ("%s %s" % (base, path)).lower()
    return any(token in haystack for token in _BUNDLE_HINTS)


def _linux_runtime_paths(target):
    """SONAME -> resolved path for every Swift library ldd can see."""
    try:
        output = subprocess.check_output(
            ["ldd", target], text=True, stderr=subprocess.STDOUT)
    except (OSError, subprocess.CalledProcessError) as exc:
        _fail("ldd could not read %s: %s" % (target, exc))
    found = {}
    for line in output.splitlines():
        if "=>" not in line:
            continue
        soname, _, rest = line.strip().partition("=>")
        path = rest.strip().split()[0]
        if path in ("", "not"):
            continue
        soname = soname.strip()
        if _should_bundle(soname, path) and os.path.isfile(path):
            found[soname] = path
    return found


def _macos_runtime_paths(target):
    """install name -> resolved path for every Swift library otool can see."""
    try:
        output = subprocess.check_output(
            ["otool", "-L", target], text=True, stderr=subprocess.STDOUT)
    except (OSError, subprocess.CalledProcessError) as exc:
        _fail("otool could not read %s: %s" % (target, exc))
    found = {}
    for line in output.splitlines()[1:]:
        path = line.strip().split()[0]
        if path.startswith("/usr/lib/") or path.startswith("/System/"):
            continue
        if not os.path.isfile(path):
            continue
        name = os.path.basename(path)
        if _should_bundle(name, path):
            found[path] = path
    return found


def _copy_runtime(lib_dir, resolved):
    os.makedirs(lib_dir, exist_ok=True)
    copied = []
    for name, path in resolved.items():
        dest = os.path.join(lib_dir, os.path.basename(name))
        shutil.copy2(path, dest)
        copied.append(dest)
    return copied


def _relocate_linux(target, lib_dir):
    if shutil.which("patchelf") is None:
        message = (
            "patchelf is required to make the server relocatable.\n"
            "Install it (patchelf on Debian and Ubuntu) and try again."
        )
        if _require_relocate():
            _fail(message)
        sys.stderr.write("peregrine: %s\n" % message)
        return
    resolved = _linux_runtime_paths(target)
    if not resolved:
        message = "ldd found no Swift runtime libraries to vendor next to %s" % target
        if _require_relocate():
            _fail(message)
        sys.stderr.write("peregrine: %s\n" % message)
        return
    copied = _copy_runtime(lib_dir, resolved)
    relative = os.path.relpath(lib_dir, os.path.dirname(target))
    subprocess.check_call(["patchelf", "--set-rpath", "$ORIGIN/" + relative, target])
    for path in copied:
        subprocess.check_call(["patchelf", "--set-rpath", "$ORIGIN", path])
    sys.stderr.write(
        "peregrine: vendored %d Swift libraries into %s\n" % (len(copied), lib_dir)
    )


def _relocate_macos(target, lib_dir):
    resolved = _macos_runtime_paths(target)
    if not resolved:
        message = "otool found no Swift runtime libraries to vendor next to %s" % target
        if _require_relocate():
            _fail(message)
        sys.stderr.write("peregrine: %s\n" % message)
        return
    copied = _copy_runtime(lib_dir, resolved)
    relative = os.path.relpath(lib_dir, os.path.dirname(target))
    # -add_rpath fails if the path is already there; that is not an error.
    add = subprocess.run(
        ["install_name_tool", "-add_rpath", "@loader_path/" + relative, target],
        capture_output=True, text=True)
    if add.returncode != 0 and "would duplicate" not in (add.stderr or ""):
        _fail("install_name_tool -add_rpath failed: %s" % add.stderr.strip())
    for original, dest in zip(resolved.values(), copied):
        name = os.path.basename(dest)
        subprocess.check_call(
            ["install_name_tool", "-id", "@rpath/%s" % name, dest])
        subprocess.check_call(
            ["install_name_tool", "-change", original,
             "@loader_path/%s/%s" % (relative, name), target])
    sys.stderr.write(
        "peregrine: vendored %d Swift libraries into %s\n" % (len(copied), lib_dir)
    )


def _pe_imports(path):
    """The DLL names a Windows executable or DLL imports, read from its PE
    import directory. Delay-loaded imports are not followed."""
    import struct

    with open(path, "rb") as fh:
        data = fh.read()
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        return []
    sections_count = struct.unpack_from("<H", data, pe + 6)[0]
    optional_size = struct.unpack_from("<H", data, pe + 20)[0]
    optional = pe + 24
    pe32_plus = struct.unpack_from("<H", data, optional)[0] == 0x20B
    directories = optional + (112 if pe32_plus else 96)
    import_rva = struct.unpack_from("<I", data, directories + 8)[0]
    sections = [struct.unpack_from("<IIII", data, optional + optional_size + 40 * i + 8)
                for i in range(sections_count)]

    def offset(rva):
        for virtual_size, address, raw_size, raw in sections:
            if address <= rva < address + max(virtual_size, raw_size):
                return rva - address + raw
        return None

    names = []
    entry = offset(import_rva) if import_rva else None
    while entry is not None:
        name_rva = struct.unpack_from("<I", data, entry + 12)[0]
        if name_rva == 0:
            break
        start = offset(name_rva)
        names.append(data[start:data.index(b"\0", start)].decode("ascii"))
        entry += 20
    return names


def _relocate_windows(target, lib_dir, env):
    """Vendors every DLL the extension needs that a Windows machine with this
    Python does not already have: the Swift runtime and OpenSSL. Found on the
    build's PATH and followed transitively. The system's own DLLs, the C
    runtime Python ships with (vcruntime140) and python3x.dll stay where they
    are; peregrine/__init__.py adds `lib_dir` to the DLL search path."""
    system = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32").lower()
    search = [d for d in env.get("PATH", "").split(os.pathsep) if d]
    keep_out = ("api-ms-", "ext-ms-", "python", "vcruntime140")
    found = {}
    pending = [target]
    while pending:
        for name in _pe_imports(pending.pop()):
            key = name.lower()
            if key in found or key.startswith(keep_out):
                continue
            path = next((os.path.join(d, name) for d in search
                         if os.path.isfile(os.path.join(d, name))), None)
            if path is None or os.path.dirname(os.path.abspath(path)).lower().startswith(system):
                continue
            found[key] = path
            pending.append(path)
    if not found:
        message = "no Swift runtime DLLs were found to vendor next to %s" % target
        if _require_relocate():
            _fail(message)
        sys.stderr.write("peregrine: %s\n" % message)
        return
    copied = _copy_runtime(lib_dir, {os.path.basename(p): p for p in found.values()})
    sys.stderr.write("peregrine: vendored %d DLLs into %s\n" % (len(copied), lib_dir))


def _relocate(target, lib_dir, env=None):
    """Vendors the Swift runtime into `lib_dir` and points `target` at it.

    A wheel has to run on a machine that has never seen this toolchain.
    libpython stays with the user's interpreter -- the tag already promised
    that pairing.
    """
    if sys.platform.startswith("linux"):
        _relocate_linux(target, lib_dir)
    elif sys.platform == "darwin":
        _relocate_macos(target, lib_dir)
    elif _windows():
        _relocate_windows(target, lib_dir, env or os.environ)
    elif _require_relocate():
        _fail("relocating the server is not implemented on %s" % sys.platform)
    else:
        sys.stderr.write("peregrine: skipping relocate on %s\n" % sys.platform)


def _remove_earlier_output(package_dir):
    """Clears whatever a previous build left in the package being assembled.

    setuptools reuses build/lib between builds, so without this a wheel carries
    the output of every earlier build in the tree: an executable from a
    PEREGRINE_BUILD=binary build next to the extension, or a module for another
    interpreter. Only the directories and files this file itself writes are
    touched.
    """
    for stale in glob.glob(os.path.join(package_dir, "_native*")):
        os.remove(stale)
    for name in ("_swift", "_bin"):
        shutil.rmtree(os.path.join(package_dir, name), ignore_errors=True)


class BinaryDistribution(Distribution):
    """Tells setuptools the package is platform-specific; the extension is
    built by Swift rather than declared to setuptools as ext_modules."""

    def has_ext_modules(self):
        return True

    def is_pure(self):
        return False


class BuildWithSwift(build_py):
    """Compiles the server, then puts it into the package being built."""

    def run(self):
        mode = _mode()
        env = _build_environment(mode)
        _check_toolchain(mode, env)
        # One scratch directory per kind of build: the two resolve different
        # pkg-config packages, so sharing one would rebuild everything each time.
        default_scratch = ".build-install" if mode == "binary" else ".build-install-extension"
        scratch = os.environ.get("PEREGRINE_SCRATCH_PATH",
                                 os.path.join(HERE, default_scratch))
        # aviancore cannot set unsafe flags, so its exclusivity checks go off here.
        command = ["swift", "build", "-c", "release", "--scratch-path", scratch,
                   "-Xswiftc", "-enforce-exclusivity=unchecked"]
        if mode == "extension":
            command += ["--product", EXTENSION_PRODUCT]
        sys.stderr.write("peregrine: %s%s\n"
                         % ("PEREGRINE_EXTENSION=1 " if mode == "extension" else "",
                            " ".join(command)))
        # Resolved against the build environment's PATH: Windows looks up a
        # bare name on this process's PATH instead.
        command[0] = shutil.which("swift", path=env.get("PATH")) or "swift"
        result = subprocess.run(command, cwd=HERE, env=env)
        if result.returncode != 0:
            _fail("the Swift build failed; see the output above.")

        super().run()

        package_dir = os.path.join(self.build_lib, "peregrine")
        if mode == "extension":
            self._install_extension(scratch, package_dir, env)
        else:
            self._install_binary(scratch, package_dir)

    def _install_extension(self, scratch, package_dir, env):
        if _windows():
            library = "%s.dll" % EXTENSION_PRODUCT
        else:
            library = "lib%s%s" % (EXTENSION_PRODUCT,
                                   ".dylib" if sys.platform == "darwin" else ".so")
        built = os.path.join(scratch, "release", library)
        if not os.path.exists(built):
            _fail("the Swift build produced no library at %s" % built)

        os.makedirs(package_dir, exist_ok=True)
        _remove_earlier_output(package_dir)
        target = os.path.join(package_dir,
                              "_native" + sysconfig.get_config_var("EXT_SUFFIX"))
        shutil.copy2(built, target)
        _relocate(target, os.path.join(package_dir, "_swift"), env)
        loaded = _check_import(self.build_lib)
        sys.stderr.write("peregrine: %s imports as peregrine._native\n" % loaded)

    def _install_binary(self, scratch, package_dir):
        built = os.path.join(scratch, "release", BINARY)
        if not os.path.exists(built):
            _fail("the Swift build produced no binary at %s" % built)

        linked = _linked_version(built)
        if linked is None:
            _fail("the built binary did not report its embedded interpreter; "
                  "it may not have linked correctly.")
        if linked != _running_version():
            _fail(
                "the built binary embeds Python %s but this build is running\n"
                "under Python %s. The wheel would be tagged for the wrong\n"
                "interpreter and the server would serve applications with a\n"
                "different Python than the one its dependencies are installed\n"
                "for." % (linked, _running_version())
            )

        _remove_earlier_output(package_dir)
        target_dir = os.path.join(package_dir, "_bin")
        os.makedirs(target_dir, exist_ok=True)
        target = os.path.join(target_dir, BINARY)
        shutil.copy2(built, target)
        os.chmod(target, 0o755)
        # Recorded from the binary itself, so the launcher warns against the
        # interpreter that is really embedded rather than the one that built it.
        with open(os.path.join(target_dir, "interpreter.txt"), "w") as fh:
            fh.write("%s\n" % linked)
            fh.write("%s\n" % (sysconfig.get_config_var("prefix") or ""))

        _relocate(target, os.path.join(target_dir, "lib"))
        relocated = _linked_version(target)
        if relocated != linked:
            _fail(
                "the relocated binary no longer reports Python %s "
                "(got %s). The Swift runtime may not have been vendored."
                % (linked, relocated)
            )


if bdist_wheel is not None:

    class BinaryWheel(bdist_wheel):
        """Tags the wheel for the one interpreter and platform it can run on."""

        def finalize_options(self):
            super().finalize_options()
            self.root_is_pure = False

        def get_tag(self):
            _, _, platform_tag = super().get_tag()
            # cp312-cp312-<platform>: the server is compiled against this
            # exact CPython ABI (not the limited API), so anything else is a
            # mis-install rather than a graceful degradation.
            #
            # A free-threaded interpreter takes the same interpreter tag and a
            # distinct ABI tag -- cp314-cp314t -- which is what stops pip from
            # installing a GIL-built wheel into python3.14t, and the other way
            # round. They really are different builds.
            interpreter = "cp%d%d" % sys.version_info[:2]
            abi = interpreter + ("t" if _free_threaded() else "")
            return interpreter, abi, _manylinux_platform_tag(platform_tag)

    COMMANDS = {"build_py": BuildWithSwift, "bdist_wheel": BinaryWheel}
else:
    COMMANDS = {"build_py": BuildWithSwift}


setup(cmdclass=COMMANDS, distclass=BinaryDistribution)
