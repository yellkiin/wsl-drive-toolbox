# Disk occupancy diagnosis and cleanup fix

User reports best-effort scan misses background services such as XTU and eject frequently fails. Existing diagnosis inspects executable location and known EXE/VHDX resources, not disk-wide open handles. Existing WM_CLOSE cannot stop services, and the first refusal prevents remaining cleanup.

## Scope and constraints

- Add real native File-object handle scanning; do not bundle Sysinternals, install a driver or scan file contents.
- Include files, directories, volume and raw physical-disk handles using native device aliases, with path-boundary matching.
- Keep native name queries in a time-bounded helper; report inaccessible processes, unresolved names and partial scans. No claim of complete kernel/mapped-file attribution.
- Associate service records by PID, explicitly distinguish shared-host candidates from proven individual service ownership.
- Offer administrator-assisted scanning for protected service processes; user handles Windows UAC. No automatic user-process termination, forced handle closure or forced dismount.
- Normal service stop only for explicitly selected eligible services with live PID/creation validation; do not stop Windows core/security/driver services or dependent service trees.
- Continue independent approved cleanup attempts, collect outcomes, re-scan and show remaining evidence. Only Windows PnP success permits safe-unplug state.
- No live cleanup/eject in development; test read-only current occupancy plus isolated file-handle holders and fake SCM.

## Work

1. Native scanner: toolbox/disk_handles.py, tests/test_disk_handles.py. scan(disk, timeout=18)->{processes:[{pid,name,created,executable,evidence:[{source:'handle',path,kind}],handle_count}],coverage:{...},warnings:[...]}. worker_main() handles stdin JSON/streamed stdout lines for same-app worker mode. Use snapshot class64, runtime File type index, DuplicateHandle with no close-source, NtQueryObject with exact Unicode length, QueryDosDevice aliases; close all owned duplicates.
2. Backend: disk_native.py, disks.py, tests/test_disks.py. Merge existing evidence and handle results. services per process from one SCM/CIM inventory; service rows have name/display_name/pid/created/can_stop/reason. recent_blocks are target-matched Kernel-PnP225 metadata, explicitly historical. Add stop_service plus structured cleanup outcomes and refreshed remaining state.
3. Root UI and helper entry: app.py,__main__.py,disk_tasks.py,test_disk_tasks.py. Add administrator scan, evidence/coverage display, explicit service selection and one grouped cleanup confirmation; helper entry never launches GUI. Elevation uses runas and a local request/result file, not app-wide administrative mode.
4. Validation and preview2: preserve universal proxy behavior, update docs and independent bundle, run tests/review, publish source update and distinct preview2 asset after CI passes.
