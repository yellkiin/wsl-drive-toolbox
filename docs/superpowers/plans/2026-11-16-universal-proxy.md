# Universal WSL Proxy Implementation Plan

> **For agentic workers:** use subagent-driven-development or executing-plans; explicit file ownership permits parallel work.

**Goal:** Independent public preview for generic protocol discovery and reliable WSL application proxy routing.
**Architecture:** Windows controller, standalone WSL payload, stdlib local HTTP gateway, read-only application checks.
**Tech Stack:** Existing Python/Tkinter/PowerShell; no new runtime dependency.
**Spec:** ../specs/2026-11-16-universal-proxy-design.md

## Global Constraints
- Keep released main/v0.1.0 unchanged; develop feature/universal-proxy and publish preview separately.
- No broad process restarts, firewall edits, TLS bypass, credential collection or paid AI requests.
- Keep checks at boundaries; do not introduce a framework or repetitive confirmations.
- Windows Python3.10+, WSL Python3.8+.

## Review Focus
- Unknown proxy programs and separate HTTP/HTTPS system proxy settings.
- SOCKS5 remote DNS, fragmented replies and authentication rejection.
- Gateway changes upstream without breaking stable URL/new connections.
- App-specific proxy overrides and inherited NO_PROXY can invalidate a generic success claim.
- Rollback and shell restart must not leave a successful-looking but dead proxy endpoint.

## Task 1: Protocol gateway and WSL lifecycle
Own toolbox/gateway.py, toolbox/wsl_agent.py, tests/test_gateway.py, tests/test_wsl_agent.py.
- [ ] Test HTTP CONNECT, SOCKS5, plain HTTP relay, upstream switch and stop/restart with local fixtures.
- [ ] Implement gateway.open_tunnel(upstream, host, port, timeout=8) -> socket; upstream dict host/port/protocol.
- [ ] Implement gateway.prepare(home, upstream) -> plan dict (url, files mapping), ensure(home), stop(home), status(home) -> dict with running,url,counts (per target hostname).
- [ ] Integrate Manager.apply(url,codex=False,verify=True,extra_files=None,gateway_mode=False) compactly with transaction; gateway config/script rollback reconciles service. No extra_files supplied by untrusted user paths.
- [ ] Expose dispatch action='probe': protocol http/https/socks5, bounded parallel probes and partial reporting; action='connect_gateway': upstream,codex; action='gateway_status'; existing apply/rollback remain compatible.
- [ ] Add injected module support: normal imports .gateway; standalone payload uses preloaded toolbox_gateway. Module __source__ carries installable source text.

## Task 2: Generic discovery and Windows bridge
Own toolbox/proxy.py, tests/test_proxy.py.
- [ ] Test unknown system proxy, complete-listener fallback, multiple endpoints, protocol persistence and selection.
- [ ] scan(distro,user,extended=False) returns existing candidates/selected/reason/issues plus coverage. All candidates include protocol,source,process label, address identity.
- [ ] connect(... existing args ...) uses connect_gateway, returns candidate + local_url + structured network status/message. Manual candidate may carry protocol='auto'/'http'/'https'/'socks5'.
- [ ] run_agent prepends module sources to stdin: toolbox_gateway and toolbox_app_checks (if present), with __source__; no giant argv.
- [ ] diagnose(distro,user) checks current configured entry rather than choosing a different discovered proxy. Add check_apps(distro,user) -> result dict from action='apps'.
- [ ] Keep source process selector optional; don't encode fixed software lists as acceptance.

## Task 3: Applications, GUI and documentation
Root owns toolbox/app_checks.py, app.py, demo.py, __main__.py, version, README, build config and tests/test_app_checks.py/test_app.py.
- [ ] Check installed tools and explicit overrides; no credentials in results.
- [ ] app_checks.check(home,gateway_module) -> {message,tools:[{name,status,detail}],warnings}. Read config first and use real new-shell process queries; only user-triggered diagnostics access public endpoints.
- [ ] wsl_agent dispatch action='apps' delegates imported toolbox_app_checks.check(home,gateway). Root coordinates this single branch with Task1 owner.
- [ ] Replace brand dropdown default with generic source; show protocol/entry and add extended scan/application checks.
- [ ] Document stable-entry scope and first-run application restart; preserve legacy Codex optional setting.

## Task 4: Verify and publish preview
- [ ] Full tests, independent review and isolated real Bash/curl+gateway fixtures.
- [ ] Build separately named preview bundle containing gateway/app_checks payload source; keep old dist folder intact.
- [ ] Push feature branch, draft PR, CI matrix and preview release with verified artifacts and honest limitations.
