# WSL Drive Toolbox Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans. Track steps below.

**Goal:** Deliver a working Chinese Windows GUI for WSL proxy switching and safe external-drive ejection, with a public GitHub repository.

**Architecture:** Windows Python/Tkinter GUI with independent proxy and disk services. A standalone Python payload executes configuration transactions inside the selected WSL user account. Windows native APIs decide safe removal.

**Tech Stack:** Python 3.10+, Tkinter/ttk, PowerShell 5.1, ctypes Win32, unittest, optional PyInstaller.

**Spec:** ../specs/2026-11-15-wsl-drive-toolbox-design.md

## Global Constraints

- 用户已批准设计与公开发布；继续执行，不重复请求计划批准。
- 避免冗余防御性编程；在系统操作边界集中检查。
- No third-party runtime dependency for source execution.
- Never force-kill processes or bypass a Windows eject veto.
- No personal paths, usernames, credentials or device IDs in public source/examples.
- Work in the current new-project folder; no existing Git checkout to isolate.

## Review Focus

- First launch with a default root user: require an explicit Linux user selection.
- Different apps or different addresses sharing a port: verify actual endpoint and owner.
- New config vs existing unmanaged shell content: preserve unrelated user content.
- USB disk reported Fixed / drive letter reassigned: use physical device identity.
- WSL on another disk using the external mount: report and release only selected related workloads.

## Task 1: Proxy backend and Linux configuration

Files: toolbox/proxy.py, toolbox/wsl_agent.py, tests/test_proxy.py, tests/test_wsl_agent.py.

Interfaces (plain dictionaries for UI):
- list_distros() -> list[dict] with name, running, default.
- list_users(distro) -> list[str].
- scan(distro, user) -> dict with candidates (app, host, port, pid, created), selected, reason, issues.
- connect(distro, user, candidate=None, family=None, codex=False) -> dict with message and candidate; raise SelectionRequired(candidates, reason) on ambiguity.
- disable(distro, user, codex=False), rollback(distro, user), diagnose(distro, user) -> dict with message.
- open_terminal(distro, user) -> None.

- [ ] Write behavioral tests for candidate selection, real HTTP CONNECT echo, managed-block preservation, first setup, rollback and external edit conflict; run failing.
- [ ] Implement Windows detection and WSL execution with timeouts, no fixed ports or distro names; include localhost and NAT gateway checks.
- [ ] Implement atomic backed-up shell setup/apply/off/rollback, optional Codex .env and old managed file migration. Keep transaction logic compact.
- [ ] Run backend tests; report exact commands, interfaces, known limitations for review.

## Task 2: Disk service and native adapter

Files: toolbox/disks.py, toolbox/disk_native.py, tests/test_disks.py.

Interfaces:
- list_disks() -> list[dict] with id, name, size, letters (e.g. E:), plus native identity/protection fields.
- inspect_disk(disk) -> dict with processes [{pid,name,created}], distros [str], mounts [dict], warnings [str].
- eject(disk) -> dict with success (bool), message, optional veto.
- close_and_eject(disk, inspection) -> same result. GUI gets one confirmation for the displayed affected objects before calling.

- [ ] Write failing tests for identity change, protected disk, successful eject vs veto and refused closure.
- [ ] Implement physical USB disk discovery, all-volume mapping and essential protection checks once at operation boundary.
- [ ] Implement best-effort occupancy inspection, normal WM_CLOSE, related WSL sync/targeted stop and ordinary mount release; no force fallback.
- [ ] Use CM_Request_Device_EjectW as the sole success authority; test with fake adapters, read-only live inventory allowed.

## Task 3: GUI, launchers, CLI and local preferences

Files: toolbox/__init__.py, toolbox/__main__.py, toolbox/app.py, toolbox/settings.py, start.py, launch CMDs, tests/test_settings.py.

- [ ] Create a clear Chinese two-page interface with proxy/disk status, choices, action buttons, activity log and background queue.
- [ ] Wire Task 1/2 APIs; one operation at a time, errors shown plainly, explicit confirmation only for closing listed workloads.
- [ ] Add demo mode for screenshot/UI validation without system changes.
- [ ] Save local preferences in LOCALAPPDATA, never in tracked source. Verify persistence with temporary-directory tests.
- [ ] Add general launchers and CLI status/scan/connect/off/rollback. Preserve original files locally before replacement.
- [ ] Launch and inspect real GUI, check responsive controls and empty/error states.

## Task 4: Documentation, packaging, review and release

Files: README.md, LICENSE, .gitignore, pyproject.toml, scripts/build.ps1, .github/workflows/test.yml, docs/images/.

- [ ] Add Chinese usage/troubleshooting, accurate limitations, a demo screenshot, MIT license and development/build instructions.
- [ ] Run all tests, compile checks, read-only live WSL/proxy/disk diagnostics and review code.
- [ ] Fix concrete review findings, build portable application if dependencies available, inspect packaged startup.
- [ ] Initialize Git, check public content, create public wsl-drive-toolbox repository under authenticated account, push and verify remote.

## Execution notes

Proxy and disk files have independent ownership and can run in parallel under the active multi-agent directive. Root owns GUI, packaging and integration. No live disk ejection or termination of existing user workloads during testing.
