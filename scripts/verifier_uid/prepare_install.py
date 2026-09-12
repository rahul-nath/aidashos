"""Prepare an exact, reviewable installer command; never install or invoke sudo."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shlex
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PYTHON = Path("/Library/Developer/CommandLineTools/usr/bin/python3")


def _root_owned(path):
    for entry in (path, *path.parents):
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError(
                f"Python runtime is not exclusively root-owned: {entry} "
                f"(uid={info.st_uid}, mode={stat.S_IMODE(info.st_mode):04o})"
            )


@dataclass(frozen=True)
class RootPython:
    """The real isolated interpreter and prefix, never a developer-tools selector."""

    executable: Path
    prefix: Path

    def __post_init__(self):
        for path in (self.executable, self.prefix):
            if not path.is_absolute():
                raise ValueError("Python runtime paths must be absolute")
            _root_owned(path)
        if not self.executable.is_file() or not self.prefix.is_dir():
            raise ValueError("Python runtime must select an installed executable and prefix")

    def payload(self):
        return {"executable": str(self.executable), "prefix": str(self.prefix)}


def probe_python(requested):
    """Refuse mutable framework ancestry before preparing any authenticated action."""
    executable = requested.resolve(strict=True)
    _root_owned(executable)
    result = subprocess.run(
        [
            str(executable),
            "-I",
            "-S",
            "-c",
            "import ctypes,json,pathlib,ssl,sqlite3,sys; "
            "print(json.dumps({'executable':sys.executable,'prefix':sys.prefix,"
            "'version':list(sys.version_info[:2]),'isolated':sys.flags.isolated,"
            "'no_site':sys.flags.no_site}))",
        ],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    identity = json.loads(result.stdout)
    if (
        set(identity) != {"executable", "prefix", "version", "isolated", "no_site"}
        or identity["isolated"] != 1
        or identity["no_site"] != 1
        or identity["version"][0] != 3
        or identity["version"][1] < 9
    ):
        raise ValueError("installed Python must support isolated no-site Python 3.9 or newer")
    return RootPython(
        Path(identity["executable"]).resolve(strict=True),
        Path(identity["prefix"]).resolve(strict=True),
    )


# Only this fixed bootstrap runs as root. It reads the bundle once with NOFOLLOW,
# verifies the reviewed digest, and copies data to fixed root-owned destinations.
# It never imports or executes code from the caller's checkout/toolchain.
BOOTSTRAP = r"""
import base64,hashlib,json,os,plistlib,stat,sys
from pathlib import Path
if os.geteuid()!=0 or not sys.flags.isolated or not sys.flags.no_site:
    raise SystemExit("authenticated isolated system Python is required")
fd=os.open(sys.argv[1],os.O_RDONLY|os.O_NOFOLLOW)
with os.fdopen(fd,"rb") as source:
    data=source.read(1048577)
if len(data)>1048576 or hashlib.sha256(data).hexdigest()!=sys.argv[2]:
    raise SystemExit("installer bundle differs from the reviewed digest")
bundle=json.loads(data)
if (set(bundle)!={"files","operator_uid","mode","python"}
        or bundle["mode"] not in ("per-launch","service")):
    raise SystemExit("invalid fixed installer schema")
if set(bundle["files"])!={"helper","canaries","qualifier"}:
    raise SystemExit("incomplete installed helper and qualification bundle")
operator=bundle["operator_uid"]
if type(operator) is not int or not 0<operator<55000 or os.environ.get("SUDO_UID")!=str(operator):
    raise SystemExit("sudo did not authenticate the configured operator")
os.umask(0o022)
def protected(path):
    for entry in (path,*path.parents):
        info=entry.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:
            raise SystemExit("installation parent is not exclusively root-owned: "+str(entry)+
                " (uid="+str(info.st_uid)+", mode="+oct(stat.S_IMODE(info.st_mode))+")")
def directory(path,mode):
    protected(path.parent)
    if not path.exists():path.mkdir(mode=mode)
    protected(path)
python=Path(sys.executable).resolve()
prefix=Path(sys.prefix).resolve()
protected(python)
protected(prefix)
if bundle["python"]!={"executable":str(python),"prefix":str(prefix)}:
    raise SystemExit("running interpreter differs from the reviewed Python runtime")
def write(path,content):
    protected(path.parent)
    if path.exists():
        protected(path)
        if path.read_bytes()!=content:
            raise SystemExit("existing installation differs; no overwrite performed")
        return
    descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o644)
    with os.fdopen(descriptor,"wb") as target:
        target.write(content);target.flush();os.fsync(target.fileno())
helper=Path("/Library/PrivilegedHelperTools/com.aidashos.verifier-uid.py")
state=Path("/private/var/db/aidashos-verifier-uid")
directory(helper.parent,0o755)
directory(state,0o755)
directory(state/"work",0o755)
destinations={"helper":helper,
    "canaries":helper.with_name("com.aidashos.verifier-uid-canaries.py"),
    "qualifier":helper.with_name("com.aidashos.verifier-uid-qualify.py")}
for name,destination in destinations.items():
    write(destination,base64.b64decode(bundle["files"][name],validate=True))
configuration=json.dumps({"operator_uid":operator,"qualification":None},sort_keys=True).encode()
write(state/"configuration.json",configuration)
if bundle["mode"]=="service":
    destination=Path("/Library/LaunchDaemons/com.aidashos.verifier-uid.plist")
    plist={"Label":"com.aidashos.verifier-uid",
      "ProgramArguments":[str(python),"-I","-S",str(helper),"serve"],
      "EnvironmentVariables":{"PATH":"/usr/bin:/bin:/usr/sbin:/sbin"},
      "RunAtLoad":True,"KeepAlive":True,"ThrottleInterval":60,
      "Umask":18}
    write(destination,plistlib.dumps(plist,sort_keys=True))
print("Installed inactive verifier preparation. No account, sudoers rule, or service was started.")
""".strip()


def prepare(destination, mode, python=DEFAULT_PYTHON):
    if mode not in ("per-launch", "service"):
        raise ValueError("unknown installed authority mode")
    runtime = probe_python(python)
    files = {
        "helper": Path(__file__).with_name("uid_gate.py").read_bytes(),
        "canaries": Path(__file__).with_name("qualification_canaries.py").read_bytes(),
        "qualifier": Path(__file__).with_name("qualify_native.py").read_bytes(),
    }
    payload = {
        "files": {name: base64.b64encode(content).decode() for name, content in files.items()},
        "operator_uid": os.getuid(),
        "mode": mode,
        "python": runtime.payload(),
    }
    content = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    destination.mkdir(parents=True, exist_ok=True)
    bundle = destination / (mode + ".json")
    bundle.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    command = shlex.join(
        (
            "/usr/bin/sudo",
            str(runtime.executable),
            "-I",
            "-S",
            "-c",
            BOOTSTRAP,
            str(bundle.resolve()),
            digest,
        )
    )
    (destination / (mode + "-install-command.txt")).write_text(command + "\n")
    return {
        "bundle": str(bundle),
        "sha256": digest,
        "file_sha256": {
            name: hashlib.sha256(content).hexdigest() for name, content in files.items()
        },
        "command": str(destination / (mode + "-install-command.txt")),
        "python": runtime.payload(),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--mode", choices=("per-launch", "service"), required=True)
    parser.add_argument("--python", type=Path, default=DEFAULT_PYTHON)
    args = parser.parse_args()
    try:
        prepared = prepare(args.destination, args.mode, args.python)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.error(f"runtime preflight failed before installation: {exc}")
    print(json.dumps(prepared, sort_keys=True))
