# macOS verifier setup

The contained test verifier uses this helper to run commands under separate macOS user identities.
The helper ships with the source, but each machine still needs administrator-approved installation, native qualification and service activation.
Installing the files alone does not enable verification: the configuration starts with `qualification: null` and refuses workload launch until qualification succeeds.

## Two concrete installation options

1. **Authenticated invocation for native qualification.** Install the fixed helper and protected state, then invoke `/Library/PrivilegedHelperTools/com.aidashos.verifier-uid.py` through sudo with the packet's pinned Python interpreter and `-I -S`, using host-owned control pipes.
   This needs authentication whenever sudo's applicable timestamp is absent or expires.
2. **Persistent authority for normal use, recommended.** Install the identical helper plus the fixed `com.aidashos.verifier-uid` launchd property list.
   Its Unix socket authenticates the operator using kernel peer credentials.
   Workload UIDs cannot connect to this socket.
   The installer does not bootstrap the service; activation follows reviewed native qualification.

Neither option installs a sudoers allowance, accepts an arbitrary root command, or uses an operator-writable Python/uv executable as root.
Preparation probes the already-installed Command Line Tools Python with `-I -S` and pins its real executable and framework prefix in the reviewed bundle.
Both paths and every ancestor must be exclusively root-owned.
The `/usr/bin/python3` developer-tools selector may resolve beneath admin-writable `/Applications`, which does not satisfy that contract.
Use `--python` only to select another already-installed runtime that satisfies the same checks.
An unavailable or unsafe runtime fails before packet creation; preparation never downloads or changes permissions.
The generated bootstrap rechecks the pinned runtime, reads bundle bytes once without following a final symlink, verifies their reviewed SHA-256, and writes only fixed root-owned destinations.
Existing identical files are retained; differing files are refused.
No Directory Services account is created.
The native tool checks cover existing uv, Python imports and process creation, and Node process creation under an otherwise unused numeric UID.
That UID has no passwd entry; Python `pwd` lookups and Node `os.userInfo()` are outside those checked capabilities.
The unchanged registered gate must establish compatibility with its actual commands.
Named account allocation is a separate decision only if a required command proves it necessary.

Generate the two review packets with the existing environment, without installing:

```sh
uv run --offline --no-sync python scripts/verifier_uid/prepare_install.py /private/tmp/aidashos-uid-install-review --mode per-launch
uv run --offline --no-sync python scripts/verifier_uid/prepare_install.py /private/tmp/aidashos-uid-install-review --mode service
```

The resulting `*-install-command.txt` contains the complete proposed privileged action.
It is a review artifact, not an instruction to execute without authorization.

## Ownership contract

Each authenticated connection owns one gate GID and independently cancelable launch UIDs.
The host supplies a source digest, at most the remaining registered 3,600-second budget, a hard per-UID process cap, and immutable command specifications.
One slot is reserved for a serialized cleanup signaler; untrusted code inherits a hard `RLIMIT_NPROC` of cap minus one.
It cannot choose a UID.
The helper binds exact argv, cwd, environment, rendered profile plus mandatory identity rules, and host-validated source digest in a protected manifest before admission.
The launched frame returns `requested_digest` for the original canonical specification and `digest` for the sealed specification with mandatory rules.
The trusted host verifier remains responsible for content-verifying the source digest.
This helper provides launch and cleanup evidence, not independent source or test-success attestation.

The root branch executes no project hooks, Git command, shebang, Python import or caller-selected executable.
The child creates its session, drops supplementary groups and real/effective/saved identity, applies Seatbelt using its trusted startup environment, then adopts the declared environment/cwd and execs the exact argv.
Root-owned HOME and scratch parents prevent arbitrary caller paths from becoming privileged chmod/chown targets.
The root control pipe and lock descriptors never reach executed code.

The inherited lock stays open until a forked root child drops identity.
If its parent dies, authenticated recovery cannot acquire the lease lock and report an empty UID while that child still has authority to launch later.
Normal cancellation, host disconnect and deadline close admission and drain owned work.
The helper kills its unreaped direct launcher children before cleanup, preventing a delayed root child from acquiring the launch UID after closure.
Cleanup uses a permanently dropped signaler so kernel UID authorization, rather than a root PID check/kill race, chooses targets.
`proc_listpids` receives a non-null one-PID buffer; both real and effective UID queries must return zero with cleared errno remaining zero.
It includes live and zombie processes in the kernel-locked query.
The helper reaps its direct children; launchd owns reaping reparented descendants.
No receipt claims that the helper reaped processes it did not parent.

The durable lease starts quarantined before a process can launch.
Only closed admission, accounted direct/root-launcher children, and kernel-confirmed zero membership allow a cleanup receipt.
UIDs are never reused by this preparation, including cleaned leases.
After abrupt helper death, a missing receipt is incomplete cleanup, never success.
An authenticated recovery request selects only a protected existing lease in the fixed UID range and refuses an active owner.
The explicit `recover UID` invocation retains this cleanup-only authority after qualification expires or its kernel binding changes.
It still requires immutable installed ownership and authenticated operator identity; service and workload launches remain qualification-gated.
Its scope is signaling and proving zero for that lease; it cannot launch commands or choose an arbitrary principal.
Scratch and manifests are retained for review instead of recursively deleting caller-influenced filesystem content as root.
Aggregate memory enforcement is explicitly unqualified.

## Staging and broker integration

One UID shared by every broker launch cannot satisfy the suite's intentional child-timeout tests: killing that UID would also kill the parent pytest process.
The helper allocates an exclusive UID per prepared launch, binds a unique occurrence handle, and aggregates cleanup receipts at gate close.
Terminating a handle sends SIGTERM; canceling it forces cleanup of its UID and logical descendants, while preserving its parent and siblings.
Canceling one launch must leave the parent and its siblings running.

Cross-UID access to existing mode-0700 temporary directories and mode-0600 files is not solved by a common umask or GID alone.
Staging uses a helper-created immutable gate anchor and host-allocated gate GID.
The operator copies source and existing toolchains into the fixed `source` and `toolchain` directories.
The helper creates fresh root launch resources; later child resources are created beneath the authenticated parent's declared scratch through pinned directory descriptors.
Each existing component must be a directory owned by the operator, root, or a UID already assigned to that gate.
Symlinks and parent traversal are refused.
Inherited ACLs are installed only on fresh helper-created directory FDs, then ownership is dropped to the launch UID.
The host broker must additionally check that its parent policy authorizes the selected scratch before asking the helper to prepare resources.
It must not grant the root helper permission to chmod/chown arbitrary paths supplied by tested code.
The staging surface is implemented but its alternate-UID access and relocated-toolchain execution remain native qualification requirements.
No existing user home or toolchain path receives a new ACL.
Real unprivileged fixtures verified inherited ACL retention through mode-0700 directory and mode-0600 file creation.
Broker wiring and privileged qualification remain separate from these fixtures.

## Operator preflight

Before requesting administrator authentication, run `stage_preflight.py` with the candidate project, its already-installed operator Python, and a fresh log in a protected operator-owned directory.
It uses the native qualifier's exact staging environment, including its fixed PATH, rather than the interactive shell's tool selection.
For example:

```sh
mkdir -m 700 /private/tmp/aidashos-stage-review
uv run --offline --no-sync python scripts/verifier_uid/stage_preflight.py \
  --project /absolute/project \
  --operator-python /absolute/installed/environment/bin/python \
  --log /private/tmp/aidashos-stage-review/preflight.log
```

The preflight orchestrates two separate phases: installed-toolchain staging, then ordinary-user command compatibility.
`verification_toolchain_staging.py` owns copied bytes, relocation, symlink permissions, provenance, and copied-tree cleanup.
`toolchain_compatibility.py` owns executable probes and their result validation, and can check an existing staged toolchain without restaging it.
Its first four commands mirror the frozen native qualifier, including `uv run` spawning the staged Python; Git and observed Python identity receive additional checks.
Behavioral parity tests prevent that command set from silently diverging.
Zero exit status without the required observable result is a failed compatibility probe.
The preflight retains separate phase outcomes and both output streams, then removes its temporary copies.
Mach-O discovery follows declared load commands and runpaths into exact files; relocation preserves native sibling-library layout without granting installation directories.
Unknown references and uncertain shared-cache search order fail closed with the requesting image and path.
A successful preflight is ordinary-user compatibility evidence only.
It does not run the helper, exercise an alternate UID, or mint a native qualification receipt.
The installed native qualifier remains the integration boundary: all 12 native checks and cleanup are mandatory before activation.
Operator-side staging and compatibility evidence never replace those checks.

Darwin authorizes explicit `readlink` separately from following a symlink during execution.
Fresh relocated links must belong to the staging operator and gate group, and retain group-read permission even under umask `077`.
Only the link is changed, using no-follow chmod; the interpreter target and installed source remain unchanged.
The staged link mode is recorded in provenance.

## Inactive helper repair

`prepare_repair.py` prepares only the current fixed helper replacement plan.
It does not install anything or run native qualification.
This repair admits the host renderer's existing `tcp` symbol without changing its TCP transport, endpoint, or mandatory identity restrictions.
The generated authenticated command requires the exact predecessor or exact already-repaired bytes, unchanged qualifier/canary siblings from that fixed plan, an inactive helper service, and exclusive existing allocator/owner locks.
It requires this operator's qualification to bind the exact predecessor and current kernel.
Every reserved UID must have a protected, identity-bound cleaned lease and kernel-confirmed zero real/effective UID members, including zombies.
The fixed protected predecessor supplies that read-only kernel predicate; replacement or checkout code never executes during repair.
Unclosed leases or unavailable kernel observations require separate recovery and are refused without attempting cleanup.
The operator must not run standalone qualification or recovery concurrently with this repair.
The command retains a protected predecessor backup and atomically replaces only the helper, preserving the qualifier, canaries, configuration, allocation state, leases, and staging directories.
The preserved qualification's old helper hash refuses new launches until native qualification binds the replacement.
Helper repair is the default; `--component helper` is an accepted explicit spelling.
The current tool accepts only schema-2 helper packets with its fixed predecessor and refuses old qualifier plans, schema-1 packets, unknown formats, and packet-supplied destinations or sibling plans.
Previously reviewed commands contain their own frozen bootstrap and do not import this preparation tool.

The operator-controlled phases are separate:

1. Prepare and review the packet while the installed service may remain active.
2. Revoke and drain owned gates through their supported owners, verify aggregate cleanup, and then stop the service using the separately reviewed operator action.
3. Run the inactive replacement command; it never stops the service or changes cleanup state.
4. Run all native qualification checks against the replacement helper and fixed siblings.
5. Bootstrap the service only after qualification succeeds.

Preparation does not establish quiescence, and the repair command refuses active service or owner state.
No phase resets the allocator, deletes leases, or recursively modifies staging.

Darwin's kernel group vector includes the effective GID, while Python's `os.getgroups()` can consult directory-service membership.
Identity admission therefore queries the plain libc `getgroups` symbol with a one-slot buffer and requires exactly the assigned GID; extra kernel groups fail instead of being truncated.
Real/effective UID and GID checks and both saved-root regain refusals are still required before execution.
Child launch failures retain a bounded phase and error code without serializing command arguments, environment values, or traceback locals.

## Qualification and evidence

The external-job probe reads native `launch_msg` replies through the installed Apple `launch.h` ABI.
A successful ordinary-operator baseline must submit its fixed `/usr/bin/true` job, observe the exact label, remove it and confirm `ESRCH`.
`RemoveJob` may report `EINPROGRESS`; cleanup still requires an observed absence within its bound.
The unchanged UID helper policy must then produce native `EPERM` or `EACCES`, with the label absent before and after the attempted submission.
Each query and removal runs in the submitting process's own UID and bootstrap context.
Missing replies, other errors, nonzero process exits and timeouts remain `OtherFailure`, regardless of diagnostic wording.
Both streams, process status and the fresh label/UID are retained before classification, including interrupted launches.
A bounded TERM grace period lets the fixed canary clean up; a timeout never certifies cleanup or qualification.
Ordinary-user controls do not replace the mandatory exclusive-UID native qualification.

Root-owned test fixtures are assembled in a private directory and published with explicit mode0755 only after setup succeeds.
Creation modes alone are insufficient because the operator's private umask removes search permissions needed by the test UID.
Disposable copies of the system identity executable receive a local ad-hoc signature because the original Apple signature can constrain the original launch location.
Signing uses only protected fixed tools, a restricted environment, no signing identity or timestamp service, and the private fixture directory as its working directory.
Each signature is verified before set-id permissions are applied to the final pinned regular file.
Setup failure keeps the directory private and clears any already-applied set-id bits.
The system executable, global signing policy, ordinary baseline, and four required sandbox denials remain unchanged.

Run the operator wrapper without whole-script sudo.
It must refuse a root EUID before invoking uv or Git and authenticate only its fixed privileged steps.

Unprivileged tests cover schema/source/argv binding, illegal UID selection, mandatory policy vocabulary, errno-sensitive zero queries, closed admission, inherited lock exclusion, real fork/refused-drop/reap, high descriptor closure after a lowered soft limit, linked gate recovery, ACL inheritance, and system-interpreter refusal of an unauthenticated entrypoint.
A separate process/pipe fixture simulates identity and containment while verifying child-only cancellation, parent survival, SIGTERM delivery, unique routing handles, and output-before-terminal ordering.
That simulation is not native credential or sandbox evidence.
These do not prove a successful alternate-UID launch.

The native qualification receipt must bind the exact installed helper hash and kernel release and contain all 12 required checks: set-id exec and spawn, persona spawn, thread credentials, real per-UID process limit, detached cleanup, normal uv/Python/Node, denied read canaries, parent disconnect, interrupted-helper recovery, cross-UID inherited staging ACLs, relocated installed toolchain, and denied external job creation.
Seatbelt already denied controlled same-user set-id executable probes under the inspected host's default policy, but alternate-UID execution and persona attributes remain unproven.
The mandatory syscall rules alone do not prove posix_spawn credential behavior.
No unsupported assumption is promoted into a passed receipt.
After these controls and staging work pass, run the unchanged registered gate with its existing 3,600-second limit.

The atomic query contract follows Apple's [proc_info.c](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/kern/proc_info.c), including live and zombie membership, and the [libproc wrapper](https://github.com/apple-oss-distributions/xnu/blob/main/libsyscall/wrappers/libproc/libproc.c), which converts syscall errors into zero while retaining errno.
Current-host read-only queries verify the ABI behavior; the exact installed kernel source tag was unavailable, so the source trace is a qualification input, not a substitute for native adversarial tests.
