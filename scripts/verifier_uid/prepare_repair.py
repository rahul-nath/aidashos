"""Prepare the fixed inactive helper repair, without installing it."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shlex
from pathlib import Path

from prepare_install import DEFAULT_PYTHON, probe_python

# Packets carry reviewed bytes, never a selectable destination, predecessor
# authority, or set of siblings allowed to change.
HELPER_PLAN = {
    "source": "uid_gate.py",
    "installed": "com.aidashos.verifier-uid.py",
    "predecessor": "4694c1275c82fd8e9a859bc29a3ffa7e893eab7d34c950be83d9754ac2bcc06a",
    "frozen": {
        "qualify_native.py": [
            "com.aidashos.verifier-uid-qualify.py",
            "091385e251331d964a56af900f069cc4b3c840edff4a239c0814f912dc6632fd",
        ],
        "qualification_canaries.py": [
            "com.aidashos.verifier-uid-canaries.py",
            "eadeca5ade0ab6e162e246e95c0632bc800c42ae58dbe01118505911db7b51f9",
        ],
    },
}

# This self-contained program imports only the protected interpreter's standard
# library. The reviewed replacement is data until this program has finished.
# The predecessor has no import-to-reservation lock. Fixed installed entrypoint
# processes are therefore also checked before replacement; this is an inactive
# operator-directed repair, not a concurrent privileged software-update service.
BOOTSTRAP = r"""
import base64,ctypes,errno,fcntl,hashlib,json,os,stat,subprocess,sys,types
from pathlib import Path

HELPER=Path("/Library/PrivilegedHelperTools/com.aidashos.verifier-uid.py")
STATE=Path("/private/var/db/aidashos-verifier-uid")
CONFIG=STATE/"configuration.json"
HELPER_PLAN=__HELPER_PLAN__

def refuse(message):
    raise SystemExit(message)

def sha(content):
    return hashlib.sha256(content).hexdigest()

def protected(path):
    for entry in (path,*path.parents):
        info=entry.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:
            refuse("repair requires exclusively root-owned ancestry: "+str(entry))

def read_file(path,privileged=True,limit=1048576):
    if privileged:protected(path)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,"rb") as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
            refuse("repair input must be a single regular file")
        content=stream.read(limit+1)
    if len(content)>limit:refuse("repair input exceeded its bound")
    return content

def service_inactive():
    result=subprocess.run(
        ["/bin/launchctl","print","system/com.aidashos.verifier-uid"],
        env={"PATH":"/usr/bin:/bin:/usr/sbin:/sbin"},
        capture_output=True,text=True,timeout=10,
    )
    absent='Could not find service "com.aidashos.verifier-uid" in domain for system'
    if result.returncode!=113 or absent not in result.stderr.splitlines():
        refuse("helper service is active or its inactive state could not be established")

def process_arguments(pid):
    # KERN_PROCARGS2 preserves argument boundaries. Parsing ps's rendered args
    # would confuse inline bootstrap code or a sudo wrapper with a script launch.
    library=ctypes.CDLL("/usr/lib/libSystem.B.dylib",use_errno=True)
    query=library.sysctl
    query.argtypes=(ctypes.POINTER(ctypes.c_int),ctypes.c_uint,ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),ctypes.c_void_p,ctypes.c_size_t)
    query.restype=ctypes.c_int
    mib=(ctypes.c_int*3)(1,49,pid)
    size=ctypes.c_size_t()
    for buffer in (None,"allocate"):
        if buffer is not None:
            if not 4<=size.value<=1048576:refuse("process argument size is invalid")
            buffer=ctypes.create_string_buffer(size.value)
        ctypes.set_errno(0)
        if query(mib,3,buffer,ctypes.byref(size),None,0)!=0:
            number=ctypes.get_errno()
            if number==errno.ESRCH:return ()
            raise OSError(number,"cannot establish installed helper process quiescence")
    raw=buffer.raw[:size.value]
    argc=ctypes.c_int.from_buffer_copy(raw[:4]).value
    if argc==0:return ()
    if not 0<argc<=65536:refuse("process argument count is invalid")
    end=raw.find(b"\0",4)
    if end<0:refuse("process executable boundary is missing")
    start=end+1
    while start<len(raw) and raw[start]==0:start+=1
    fields=raw[start:].split(b"\0")
    if len(fields)<=argc:refuse("process argument boundaries are incomplete")
    return tuple(os.fsdecode(field) for field in fields[:argc])

def python_executable(path):
    name=Path(path).name.lower()
    return name=="python" or (name.startswith("python") and name[6:].replace(".","").isdigit())

def installed_script(arguments):
    if not arguments or not python_executable(arguments[0]):return None
    index=1
    while index<len(arguments):
        value=arguments[index]
        if value in ("-c","-m","-"):return None
        if value in ("-W","-X","--check-hash-based-pycs"):
            index+=2;continue
        if value=="--":index+=1;break
        if not value.startswith("-"):break
        index+=1
    if index>=len(arguments):return None
    name=Path(arguments[index]).name
    if name in (HELPER.name,"com.aidashos.verifier-uid-qualify.py"):return name
    return None

def python_processes(observation):
    processes=[]
    for line in observation.splitlines():
        fields=line.split(maxsplit=2)
        if len(fields)!=3 or not all(field.isdecimal() for field in fields[:2]):
            refuse("invalid process ownership observation")
        pid,uid=map(int,fields[:2])
        if python_executable(fields[2]):processes.append((pid,uid))
    return processes

def no_installed_processes():
    result=subprocess.run(["/bin/ps","-axo","pid=,uid=,comm="],
        env={"PATH":"/usr/bin:/bin"},capture_output=True,text=True,timeout=10)
    if result.returncode!=0:refuse("cannot enumerate installed helper owners")
    for pid,uid in python_processes(result.stdout):
        if uid!=0 or pid==0 or pid==os.getpid():continue
        entrypoint=installed_script(process_arguments(pid))
        if entrypoint is not None:
            refuse("installed helper entrypoint is still running: pid="+str(pid)+" "+entrypoint)

def acquire_lock(path):
    protected(path)
    fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
            refuse("helper ownership lock is not a single regular file")
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BaseException:
        os.close(fd)
        raise
    return fd

def lock_existing_owners():
    # Holding the existing allocator closes new reservations while all existing
    # owner locks also cover forked root launchers. Never create missing locks.
    descriptors=[]
    try:
        descriptors.append(acquire_lock(STATE/"allocation.lock"))
        for directory in sorted(STATE.iterdir()):
            if not directory.name.startswith("uid-"):continue
            suffix=directory.name[4:]
            if not suffix.isdecimal() or str(int(suffix))!=suffix:
                refuse("unrecognized exclusive UID reservation")
            protected(directory)
            if not directory.is_dir():refuse("exclusive UID reservation is not a directory")
            descriptors.append(acquire_lock(directory/"owner.lock"))
        return descriptors
    except BaseException:
        for fd in reversed(descriptors):os.close(fd)
        raise

def predecessor_contract(content):
    # Only these protected, fixed-hash bytes supply the existing read-only kernel
    # zero predicate. No replacement or checkout code executes during repair.
    if sha(content)!=HELPER_PLAN["predecessor"]:
        refuse("cleanup contract is not the fixed protected predecessor")
    module=types.ModuleType("_aidashos_repair_predecessor")
    module.__file__=str(HELPER)
    sys.modules[module.__name__]=module
    try:exec(compile(content,str(HELPER),"exec"),module.__dict__)
    finally:sys.modules.pop(module.__name__,None)
    return module

def validate_configuration(config,operator,contract):
    if (not isinstance(config,dict) or set(config)!={"operator_uid","qualification"}
            or type(config["operator_uid"]) is not int or config["operator_uid"]!=operator):
        refuse("repair requires the existing authenticated operator configuration")
    qualification=config["qualification"]
    if (not isinstance(qualification,dict)
            or set(qualification)!={"helper_sha256","kernel_release","checks"}
            or qualification["helper_sha256"]!=HELPER_PLAN["predecessor"]
            or qualification["kernel_release"]!=os.uname().release
            or not isinstance(qualification["checks"],list)
            or any(not isinstance(check,str) for check in qualification["checks"])
            or set(qualification["checks"])!=contract.QUALIFICATION_CHECKS):
        refuse("repair requires qualification bound to the exact predecessor and current kernel")

def require_closed_reservations(contract):
    # The allocator and every existing owner lock are held before these reads.
    # Never signal or repair a lease here: uncertainty requires separate recovery.
    membership=contract.DarwinMembership()
    for directory in sorted(STATE.iterdir()):
        if not directory.name.startswith("uid-"):continue
        uid=int(directory.name[4:])
        if not contract.FIRST_UID<=uid<=contract.LAST_UID:
            refuse("reservation UID is outside the fixed helper range")
        manifest=json.loads(read_file(directory/"lease.json",limit=contract.MAX_MANIFEST_BYTES))
        if (not isinstance(manifest,dict) or manifest.get("schema")!=contract.SCHEMA
                or type(manifest.get("uid")) is not int or manifest["uid"]!=uid
                or manifest.get("status")!="cleaned"):
            refuse("reserved UID lacks its existing closed cleanup evidence: "+str(uid))
        if not membership.empty(uid):
            refuse("reserved UID still has live or zombie processes: "+str(uid))

def sync_directory(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(fd)
    finally:os.close(fd)

def write_new(path,content,mode):
    protected(path.parent)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
    try:
        with os.fdopen(fd,"wb") as stream:
            os.fchmod(stream.fileno(),mode)
            stream.write(content);stream.flush();os.fsync(stream.fileno())
    except BaseException:
        path.unlink()
        raise

def ensure_backup(backup,original):
    if backup.exists():
        if read_file(backup)!=original:refuse("protected predecessor backup differs")
    else:
        write_new(backup,original,0o600)
    sync_directory(backup.parent)

def replace_component(target,replacement):
    temporary=target.with_name("."+target.name+".repair-"+os.urandom(12).hex())
    created=False
    try:
        write_new(temporary,replacement,0o644)
        created=True
        os.replace(temporary,target)
        created=False
        sync_directory(target.parent)
    finally:
        if created:temporary.unlink()

def selected_repair(bundle):
    if not isinstance(bundle,dict) or type(bundle.get("schema")) is not int:
        refuse("invalid fixed repair schema")
    if bundle["schema"]!=2:
        refuse("unsupported fixed repair schema")
    if (set(bundle)!={"schema","operator_uid","python","predecessor_sha256",
            "component","replacement_sha256","replacement"}
            or bundle["component"]!="helper"):
        refuse("only fixed helper repair packets are supported")
    digest,encoded=bundle["replacement_sha256"],bundle["replacement"]
    if bundle["predecessor_sha256"]!=HELPER_PLAN["predecessor"]:
        refuse("repair predecessor differs from the fixed helper plan")
    replacement=base64.b64decode(encoded,validate=True)
    if not replacement or sha(replacement)!=digest or digest==HELPER_PLAN["predecessor"]:
        refuse("replacement helper digest is invalid or unchanged")
    return digest,replacement

def run(bundle_path,expected_digest):
    if (os.geteuid()!=0 or sys.platform!="darwin"
            or not sys.flags.isolated or not sys.flags.no_site):
        refuse("authenticated isolated macOS Python is required")
    raw=read_file(bundle_path,privileged=False)
    if sha(raw)!=expected_digest:refuse("repair bundle differs from the reviewed digest")
    bundle=json.loads(raw)
    replacement_digest,replacement=selected_repair(bundle)
    target=HELPER.with_name(HELPER_PLAN["installed"])
    predecessor=HELPER_PLAN["predecessor"]
    backup=target.with_name(target.name+".previous-"+predecessor)
    operator=bundle["operator_uid"]
    if (type(operator) is not int or not 0<operator<55000
            or os.environ.get("SUDO_UID")!=str(operator)):
        refuse("sudo did not authenticate the reviewed operator")
    python,prefix=Path(sys.executable).resolve(),Path(sys.prefix).resolve()
    protected(python);protected(prefix)
    if bundle["python"]!={"executable":str(python),"prefix":str(prefix)}:
        refuse("running interpreter differs from the reviewed Python runtime")
    protected(STATE)
    no_installed_processes()
    descriptors=lock_existing_owners()
    try:
        service_inactive()
        configuration=read_file(CONFIG)
        for name,digest in HELPER_PLAN["frozen"].values():
            if sha(read_file(HELPER.with_name(name)))!=digest:
                refuse("installed sibling differs from the fixed repair plan")
        original=read_file(target)
        if sha(original) not in (predecessor,replacement_digest):
            refuse("installed component is not the exact reviewed predecessor or replacement")
        contract=predecessor_contract(original if sha(original)==predecessor else read_file(backup))
        validate_configuration(json.loads(configuration),operator,contract)
        require_closed_reservations(contract)
        if sha(original)==replacement_digest:
            if sha(read_file(backup))!=predecessor:
                refuse("repaired component lacks its exact protected predecessor backup")
            sync_directory(target.parent)
            print("Exact helper repair already present; no files changed.")
            return
        ensure_backup(backup,original)
        # Recheck the protected bytes immediately before the sole replacement.
        if read_file(target)!=original or read_file(CONFIG)!=configuration:
            refuse("installed component or configuration changed during repair")
        no_installed_processes()
        require_closed_reservations(contract)
        replace_component(target,replacement)
        print("Helper repaired; allocator, leases, staging, and "
            "configuration preserved. Its predecessor hash cannot admit the new helper; "
            "native qualification is required before restarting the service.")
    finally:
        for fd in reversed(descriptors):os.close(fd)

if __name__=="__main__":
    if len(sys.argv)!=3:refuse("expected only a reviewed bundle and its digest")
    os.umask(0o077)
    run(Path(sys.argv[1]),sys.argv[2])
""".strip().replace("__HELPER_PLAN__", repr(HELPER_PLAN))


def prepare(destination, python=DEFAULT_PYTHON):
    if not destination.is_absolute():
        raise ValueError("repair packet destination must be absolute")
    runtime = probe_python(python)
    source = Path(__file__).parent
    for name, (_, expected) in HELPER_PLAN["frozen"].items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != expected:
            raise ValueError("sibling source differs from the fixed repair plan")
    replacement = (source / HELPER_PLAN["source"]).read_bytes()
    replacement_digest = hashlib.sha256(replacement).hexdigest()
    if not replacement or replacement_digest == HELPER_PLAN["predecessor"]:
        raise ValueError("helper repair has not changed the predecessor")
    payload = {
        "schema": 2,
        "component": "helper",
        "operator_uid": os.getuid(),
        "python": runtime.payload(),
        "predecessor_sha256": HELPER_PLAN["predecessor"],
        "replacement_sha256": replacement_digest,
        "replacement": base64.b64encode(replacement).decode(),
    }
    content = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(content).hexdigest()
    destination.mkdir(mode=0o700)
    bundle = destination / "repair.json"
    command = destination / "repair-command.txt"
    for path, data in (
        (bundle, content),
        (
            command,
            (
                shlex.join(
                    (
                        "/usr/bin/sudo",
                        str(runtime.executable),
                        "-I",
                        "-S",
                        "-c",
                        BOOTSTRAP,
                        str(bundle),
                        digest,
                    )
                )
                + "\n"
            ).encode(),
        ),
    ):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    return {
        "bundle": str(bundle),
        "sha256": digest,
        "component": "helper",
        "replacement_sha256": replacement_digest,
        "predecessor_sha256": HELPER_PLAN["predecessor"],
        "command": str(command),
        "python": runtime.payload(),
        "precondition": (
            "Helper service inactive; no concurrent qualification or recovery; all reserved "
            "UID leases cleaned, owner locks exclusive and kernel membership empty."
        ),
        "native_qualification": "still required; this command does not start the service",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--python", type=Path, default=DEFAULT_PYTHON)
    parser.add_argument(
        "--component",
        choices=("helper",),
        default="helper",
        help="only helper repair is supported",
    )
    args = parser.parse_args()
    print(json.dumps(prepare(args.destination, args.python), sort_keys=True))
