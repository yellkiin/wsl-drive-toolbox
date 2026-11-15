"""Safe removal policy; all machine access lives in disk_native."""

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
    return _adapter().request_eject(_validate(disk))


def _workloads(info):
    return ({(p["pid"], p["created"]) for p in info["processes"]},
            set(info["distros"]),
            {(m["distro"], m["target"], m["source"]) for m in info["mounts"]})


def close_and_eject(disk, inspection):
    """The caller confirms the displayed inspection before invoking this method.

    New workloads need a new confirmation. Closing and removal have separate
    safety boundaries because saving an application can take arbitrary time.
    """
    current = _validate(disk)
    native = _adapter()
    fresh = native.inspect(current)
    if any(not found <= accepted for found, accepted in zip(_workloads(fresh), _workloads(inspection))):
        return {"success": False, "message": "占用情况已改变，请重新查看并确认受影响对象。"}
    for process in fresh["processes"]:
        if not native.close_process(process):
            return {"success": False, "message": f"{process['name']} 未正常退出；请完成保存或手动关闭后重试。"}
    for mount in fresh["mounts"]:
        if mount["distro"] not in fresh["distros"] and not native.release_mount(mount):
            return {"success": False, "message": f"{mount['distro']} 的 {mount['target']} 仍忙，未强制卸载或停止发行版。"}
    for name in fresh["distros"]:
        native.stop_distro(name)
    return eject(disk)
