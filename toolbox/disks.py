"""Safe removal policy; all machine access lives in disk_native."""

from .disk_native import is_explorer

_native = None


def _adapter():
    global _native
    if _native is None:
        from .disk_native import WindowsDisks
        _native = WindowsDisks()
    return _native


def list_disks():
    """List physical external disks, including USB SSDs reported as Fixed."""
    return [disk for disk in _adapter().inventory() if disk["external"]]


def _identity(disk):
    return (disk["id"], disk["number"], disk["pnp_id"], disk.get("eject_id"),
            sorted((v["id"], tuple(sorted(v["letters"])), tuple(sorted(v["disks"])),
                    tuple(sorted(v.get("paths", []))))
                   for v in disk["volumes"]))


def _validate(disk):
    current = next((d for d in _adapter().inventory() if d["id"] == disk["id"]), None)
    if current is None or _identity(current) != _identity(disk):
        raise RuntimeError("设备身份或卷映射已改变，请重新检测硬盘。")
    if not current["protection_known"]:
        raise RuntimeError("无法确认系统盘、分页文件或程序所在盘，已停止操作。")
    if current["protected"]:
        raise RuntimeError("此硬盘受到保护：" + "、".join(current["protected"]))
    if not current["external"] or not current.get("eject_id"):
        raise RuntimeError("无法验证安全的外接存储移除节点，已停止操作。")
    if any(v["disks"] != [current["number"]] for v in current["volumes"]):
        raise RuntimeError("卷跨越多个磁盘或物理映射不明，不能自动弹出。")
    return current


def inspect_disk(disk):
    """Best-effort diagnostics, without starting stopped WSL distributions."""
    return _adapter().inspect(disk)


def eject(disk):
    """Only the Windows PnP removal API may authorize unplugging."""
    native = _adapter()
    current = _validate(disk)
    result = native.request_eject(current)
    if not result["success"]:
        try:
            result = dict(result, remaining=native.inspect(current))
        except (OSError, RuntimeError) as error:
            result = dict(result, diagnostics_error=str(error))
    return result


def _workloads(info):
    return ({(p["pid"], p["created"]) for p in info["processes"]},
            set(info["distros"]),
            {(m["distro"], m["target"], m["source"]) for m in info["mounts"]})


def close_and_eject(disk, inspection, selected_services=()):
    """The caller confirms the displayed inspection before invoking this method.

    New workloads need a new confirmation. Closing and removal have separate
    safety boundaries because saving an application can take arbitrary time.
    """
    current = _validate(disk)
    native = _adapter()
    fresh = native.inspect(current)
    outcomes = []
    if any(not found <= accepted for found, accepted in zip(_workloads(fresh), _workloads(inspection))):
        return {"success": False, "message": "占用情况已改变，请重新查看并确认受影响对象。",
                "outcomes": outcomes, "remaining": fresh}
    accepted_services = {s["name"].casefold(): s for s in inspection.get("services", [])}
    live_services = {s["name"].casefold(): s for s in fresh.get("services", [])}
    chosen = []
    for requested in selected_services:
        key = (requested if isinstance(requested, str) else requested["name"]).casefold()
        accepted, live = accepted_services.get(key), live_services.get(key)
        if (not accepted or not accepted.get("can_stop") or not live or not live.get("can_stop")
                or (accepted["pid"], accepted["created"]) != (live["pid"], live["created"])
                or (isinstance(requested, dict) and
                    (requested.get("pid"), requested.get("created")) != (accepted["pid"], accepted["created"]))):
            return {"success": False, "message": "所选服务状态或身份已改变，请重新查看并确认。",
                    "outcomes": outcomes, "remaining": fresh}
        if key not in {s["name"].casefold() for s in chosen}:
            chosen.append(live)

    def attempt(kind, name, action, failure, pid=None):
        try:
            value = action()
            success = bool(value.get("success")) if isinstance(value, dict) else value is not False
            message = value.get("message", "") if isinstance(value, dict) else ("已完成" if success else failure)
        except (RuntimeError, OSError) as error:
            success, message = False, str(error)
        outcomes.append(dict(kind=kind, name=name, pid=pid, success=success, message=message))

    for process in fresh["processes"]:
        if process.get("services") or not process.get("can_close", True):
            continue
        close = (lambda p=process: native.close_explorer(p, current)) if is_explorer(process) else (
            lambda p=process: native.close_process(p))
        attempt("process", process["name"], close,
                "未正常退出；请完成保存或手动关闭后重试。", process["pid"])
    for service in chosen:
        attempt("service", service["name"], lambda s=service: native.stop_service(s), "服务未停止。", service["pid"])
    for mount in fresh["mounts"]:
        if mount["distro"] not in fresh["distros"]:
            attempt("mount", f"{mount['distro']}:{mount['target']}", lambda m=mount: native.release_mount(m),
                    "挂载仍忙，未强制卸载或停止发行版。")
    for name in fresh["distros"]:
        attempt("distro", name, lambda n=name: native.stop_distro(n), "发行版未停止。")
    # A rejected close can be a save/cancel prompt. Complete independent work,
    # but do not eject underneath that prompt even if its file handle vanished.
    current = _validate(disk)
    try:
        remaining = native.inspect(current)
    except (OSError, RuntimeError) as error:
        return {"success": False, "message": f"清理后复查失败：{error}", "outcomes": outcomes}
    if any(not o["success"] for o in outcomes):
        return {"success": False, "message": "部分对象未正常释放；其余已确认对象已继续处理，请查看各项结果后重试。",
                "outcomes": outcomes, "remaining": remaining}
    # Already scanned after cleanup; keep one refresh and a final identity check.
    result = native.request_eject(_validate(disk))
    return dict(result, outcomes=outcomes, remaining=remaining)
