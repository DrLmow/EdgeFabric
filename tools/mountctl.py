#!/usr/bin/env python3
"""Reconcile explicitly declared, native Linux bind mounts per project."""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


class MountError(Exception):
    pass


TOKEN_PREFIX = "edgefabric-mountctl-"
IDENTITY_KEYS = ("id", "parent", "device", "root", "target", "fstype", "source")


def beneath(path, parent):
    return path != parent and parent in path.parents


def expand_path(value, base):
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise MountError("路径必须是非空字符串")
    value = os.path.expandvars(os.path.expanduser(value))
    if re.search(r"\$(?:\{[^}]+\}|[A-Za-z_][A-Za-z_0-9]*)", value):
        raise MountError(f"路径包含未定义的环境变量: {value}")
    return Path(os.path.abspath(base / value))


def check_target(target, root):
    if not beneath(target, root):
        raise MountError(f"目标必须位于配置文件所在目录内部: {target}")
    if target.resolve() != target:
        raise MountError(f"目标或其父目录不能是符号链接: {target}")
    if target.exists() and not target.is_dir():
        raise MountError(f"目标不是目录: {target}")


def read_json(path):
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise MountError(f"JSON 中出现重复字段: {key}")
            result[key] = value
        return result

    try:
        return json.loads(path.read_text(), object_pairs_hook=unique_keys)
    except (OSError, ValueError) as exc:
        raise MountError(f"无法读取 {path}: {exc}") from exc


def load_manifest(path):
    data = read_json(path)
    if not isinstance(data, dict) or set(data) != {"version", "mounts"}:
        raise MountError("配置必须且只能包含 version 和 mounts 字段")
    if type(data["version"]) is not int or data["version"] != 1:
        raise MountError("仅支持 version: 1")
    if not isinstance(data["mounts"], list):
        raise MountError("mounts 必须是列表；明确清空所有挂载请使用 []")
    result = {}
    root = path.parent
    for entry in data["mounts"]:
        if not isinstance(entry, dict) or set(entry) != {"source", "target"}:
            raise MountError("每个挂载条目必须且只能包含 source 和 target")
        source = expand_path(entry["source"], root).resolve()
        target = expand_path(entry["target"], root)
        check_target(target, root)
        if not source.is_dir():
            raise MountError(f"源目录不存在或无法访问: {source}")
        if str(target) in result:
            raise MountError(f"重复的目标目录: {target}")
        result[str(target)] = str(source)
    targets = [Path(t) for t in result]
    for target in targets:
        for other in targets:
            if beneath(target, other):
                raise MountError(f"不支持嵌套目标: {target} 位于 {other} 内")
        for source in map(Path, result.values()):
            if source == target or beneath(source, target) or beneath(target, source):
                raise MountError(f"源目录与挂载目标不能互相包含: {source}, {target}")
    return result


def unescape_mount(value):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), value)


def parse_mountinfo(contents):
    mounts = {}
    for line in contents.splitlines():
        left, right = line.split(" - ", 1)
        fields, tail = left.split(), right.split()
        item = {
            "id": fields[0], "parent": fields[1], "device": fields[2],
            "root": unescape_mount(fields[3]),
            "target": unescape_mount(fields[4]), "fstype": tail[0],
            "source": unescape_mount(tail[1]),
        }
        mounts.setdefault(item["target"], []).append(item)
    return mounts


def owned_mount(record, actual):
    if record.get("backend") == "bind":
        # A bind mount has no private filesystem tag. Only a recorded mount
        # identity proves ownership; samefile() alone would also match a mount
        # somebody else created at the same path.
        return record["mount"] is not None and record["mount"] == actual
    # Read-only compatibility for identifying and removing old tool-created
    # FUSE mounts. New mounts never use FUSE.
    return (
        actual["fstype"] == "fuse." + record["token"]
        and actual["root"] == "/"
        and actual["target"] == record["target"]
        and (record["mount"] is None or record["mount"] == actual)
    )


def scope_id():
    try:
        machine = Path("/etc/machine-id").read_text().strip()
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        namespace = str(os.stat("/proc/self/ns/mnt").st_ino)
        if not machine or not boot:
            raise MountError("无法确定本机身份")
        return hashlib.sha256(machine.encode()).hexdigest()[:20], boot, namespace
    except OSError as exc:
        raise MountError(f"此工具需要 Linux 挂载命名空间: {exc}") from exc


class StateStore:
    def __init__(self, config, base=None):
        if base is None:
            base = config.parent / ".mountctl"
        self.base = Path(base).expanduser().absolute()
        machine, boot, namespace = scope_id()
        self.directory = self.base / machine / str(os.getuid()) / boot / namespace
        key = hashlib.sha256(str(config.parent).encode()).hexdigest()
        self.path = self.directory / ("project-" + key + ".json")
        self.config = config
        self.legacy_paths = []

    def check_targets(self, targets):
        state_root = self.base.resolve()
        for value in targets:
            target = Path(value)
            if target == state_root or beneath(target, state_root) or beneath(state_root, target):
                raise MountError(f"挂载目标不能覆盖或位于状态目录内: {target}")

    @contextmanager
    def lock(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.directory / ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise MountError("本机当前已有挂载同步正在执行，请稍后重试") from exc
            yield

    def load(self):
        records = None
        if self.path.exists():
            data = read_json(self.path)
            if (not isinstance(data, dict) or data.get("version") not in (2, 3)
                    or data.get("project") != str(self.config.parent)
                    or not isinstance(data.get("config"), str)
                    or Path(data["config"]).parent != self.config.parent):
                raise MountError(f"状态文件格式不正确，拒绝执行: {self.path}")
            records = self.validate_records(data.get("records"))

        # Older versions keyed state by the config filename. Recognize those
        # records even after that file was renamed; dry-run must not rewrite them.
        self.legacy_paths = []
        legacy_records = {}
        for path in sorted(self.directory.glob("*.json")):
            if not re.fullmatch(r"[0-9a-f]{64}\.json", path.name):
                continue
            data = read_json(path)
            if (not isinstance(data, dict) or data.get("version") != 1
                    or not isinstance(data.get("config"), str)
                    or not Path(data["config"]).is_absolute()):
                raise MountError(f"旧状态文件格式不正确，拒绝执行: {path}")
            if Path(data["config"]).parent != self.config.parent:
                continue
            old = self.validate_records(data.get("records"))
            self.legacy_paths.append(path)
            # A successfully committed v2 file is authoritative if a process
            # stopped before deleting its v1 inputs.
            if records is not None:
                continue
            for target, record in old.items():
                if target in legacy_records and legacy_records[target] != record:
                    raise MountError(f"多个旧配置对同一目标的记录冲突，拒绝自动合并: {target}")
                legacy_records[target] = record
        return records if records is not None else legacy_records

    def validate_records(self, records):
        if not isinstance(records, dict):
            raise MountError("状态文件中的 records 必须是对象")
        for target, record in records.items():
            native = isinstance(record, dict) and record.get("backend") == "bind"
            expected = {"target", "source", "mount", "backend" if native else "token"}
            if (not isinstance(record, dict)
                    or set(record) != expected
                    or record["target"] != target
                    or not isinstance(record["source"], str)
                    or not Path(record["source"]).is_absolute()
                    or (not native and (
                        not isinstance(record["token"], str)
                        or not re.fullmatch(TOKEN_PREFIX + r"[0-9a-f]{32}", record["token"])))):
                raise MountError(f"状态记录损坏: {target}")
            target_path = Path(target)
            if not target_path.is_absolute() or str(Path(os.path.abspath(target))) != target:
                raise MountError(f"状态记录中的目标不是规范绝对路径: {target}")
            check_target(target_path, self.config.parent)
            identity = record["mount"]
            if identity is not None and (
                not isinstance(identity, dict) or set(identity) != set(IDENTITY_KEYS)
                or not all(isinstance(v, str) for v in identity.values())
                or identity["target"] != target
                or (native and identity["fstype"].startswith("fuse"))
                or (not native and identity["fstype"] != "fuse." + record["token"])
            ):
                raise MountError(f"挂载身份记录损坏: {target}")
        return records

    def save(self, records):
        data = {
            "version": 3, "project": str(self.config.parent),
            "config": str(self.config), "records": records,
        }
        fd, temporary = tempfile.mkstemp(prefix=".state-", dir=self.directory)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
                # Commit the new state durably before removing legacy records.
                for path in self.legacy_paths:
                    if path.exists():
                        path.unlink()
                self.legacy_paths = []
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


@dataclass(frozen=True)
class Action:
    kind: str
    target: str
    source: str = ""


def make_plan(desired, records, actual):
    actions = []
    managed = {}
    for target, record in records.items():
        entries = actual.get(target, [])
        if not entries:
            actions.append(Action("forget", target))
        elif record.get("backend") == "bind" and record["mount"] is None:
            raise MountError(
                f"原生挂载缺少完成记录，无法确认身份: {target}；"
                "请核对后手动正常卸载该目标，再执行 sync"
            )
        elif len(entries) != 1 or not owned_mount(record, entries[0]):
            raise MountError(f"挂载已被替换或叠加，拒绝操作: {target}")
        else:
            managed[target] = record
    for target, record in managed.items():
        if desired.get(target) != record["source"] or record.get("backend") != "bind":
            actions.append(Action("unmount", target, record["source"]))
        else:
            actions.append(Action("keep", target, record["source"]))
    for target, source in desired.items():
        if target in actual and target not in managed:
            raise MountError(f"目标已有非本工具管理的挂载，拒绝接管: {target}")
        if (target not in managed or managed[target]["source"] != source
                or managed[target].get("backend") != "bind"):
            actions.append(Action("mount", target, source))
    removing = {a.target for a in actions if a.kind == "unmount"}
    for action in actions:
        if action.kind not in {"mount", "unmount"}:
            continue
        target = Path(action.target)
        for child in actual:
            if beneath(Path(child), target) and child not in removing:
                raise MountError(f"目标内还有不应卸载的挂载: {child}")
        if action.kind == "mount" and action.target not in removing:
            if target.is_dir() and any(target.iterdir()):
                raise MountError(f"目标非空，拒绝遮盖已有文件: {target}")
    for source in desired.values():
        for target in removing:
            if Path(source) == Path(target) or beneath(Path(source), Path(target)):
                raise MountError(f"源目录依赖即将卸载的目录: {source}")
    priority = {"forget": 0, "unmount": 1, "keep": 2, "mount": 3}
    return sorted(actions, key=lambda a: (priority[a.kind], -len(Path(a.target).parts), a.target))


class SystemBackend:
    def mounts(self):
        return parse_mountinfo(Path("/proc/self/mountinfo").read_text())

    def preflight(self, actions):
        changes = [a for a in actions if a.kind in {"mount", "unmount"}]
        for action in changes:
            command = "mount" if action.kind == "mount" else "umount"
            if not shutil.which(command):
                raise MountError(f"找不到 {command}，请安装 util-linux")
            if action.kind == "mount":
                self.check_source(action.source)
        if changes and os.geteuid() != 0:
            if not shutil.which("sudo"):
                raise MountError("原生挂载需要管理员权限，请安装并配置 sudo")
            # sudo -v can still require a password with mixed PASSWD/NOPASSWD
            # rules, even when actual commands are permitted without one.
            ready = subprocess.run(
                ["sudo", "-n", "true"], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ).returncode == 0
            if not ready and subprocess.run(["sudo", "-v"]).returncode:
                raise MountError("sudo 认证失败，未修改任何挂载")

    def check_source(self, source):
        path = Path(source)
        ancestors = [
            (len(Path(target).parts), entries[-1])
            for target, entries in self.mounts().items()
            if path == Path(target) or beneath(path, Path(target))
        ]
        if not ancestors:
            raise MountError(f"无法识别源目录所在文件系统: {source}")
        filesystem = max(ancestors, key=lambda entry: entry[0])[1]
        if filesystem["fstype"].startswith("fuse"):
            raise MountError(f"源目录本身仍位于 FUSE 文件系统，请先更换来源: {source}")

    @staticmethod
    def samefile(source, target):
        return os.path.samefile(source, target)

    @staticmethod
    def run(args):
        result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True)
        if result.returncode:
            raise MountError((result.stderr or result.stdout).strip() or f"命令失败: {args[0]}")

    def privileged_run(self, args):
        if os.geteuid() != 0:
            args = ["sudo", "-n", *args]
        self.run(args)

    def mount(self, source, target):
        self.check_source(source)
        self.privileged_run(["mount", "--bind", "--", source, target])

    def unmount(self, target):
        self.privileged_run(["umount", "--", target])


def execute_plan(actions, records, store, backend, emit=print):
    failures = 0
    for action in actions:
        target = action.target
        try:
            check_target(Path(target), store.config.parent)
            actual = backend.mounts()
            entries = actual.get(target, [])
            if action.kind == "forget":
                if entries:
                    raise MountError("实际挂载状态发生变化，请重新执行")
                del records[target]
                store.save(records)
            elif action.kind in {"unmount", "keep"}:
                record = records[target]
                if len(entries) != 1 or not owned_mount(record, entries[0]):
                    raise MountError("挂载身份发生变化，拒绝操作")
                if action.kind == "keep":
                    if not backend.samefile(record["source"], target):
                        raise MountError("源目录已改变，当前源和目标不是同一目录；请核对后重新挂载")
                else:
                    if any(beneath(Path(p), Path(target)) for p in actual):
                        raise MountError("目标内还有挂载；先处理子挂载")
                    backend.unmount(target)
                    if backend.mounts().get(target):
                        raise MountError("卸载后目标仍有挂载，保留记录等待检查")
                    del records[target]
                    store.save(records)
            elif action.kind == "mount":
                if entries or target in records:
                    raise MountError("目标仍被占用或旧挂载尚未卸载")
                if any(beneath(Path(p), Path(target)) for p in actual):
                    raise MountError("目标内已有其他挂载")
                if not Path(action.source).is_dir():
                    raise MountError(f"源目录已不可访问: {action.source}")
                directory = Path(target)
                directory.mkdir(parents=True, exist_ok=True)
                if any(directory.iterdir()):
                    raise MountError("目标非空，拒绝遮盖已有文件")
                record = {
                    "source": action.source, "target": target,
                    "backend": "bind", "mount": None,
                }
                # A crash before recording the resulting mount ID must fail
                # closed on the next run, rather than adopt a foreign mount.
                records[target] = record
                store.save(records)
                try:
                    backend.mount(action.source, target)
                except (MountError, OSError):
                    if not backend.mounts().get(target):
                        del records[target]
                        store.save(records)
                    raise
                entries = backend.mounts().get(target, [])
                if (len(entries) != 1 or entries[0]["fstype"].startswith("fuse")
                        or not backend.samefile(action.source, target)):
                    raise MountError("未能验证新挂载；保留操作记录供下次检查")
                record["mount"] = entries[0]
                store.save(records)
            emit(f"{action.kind.upper():7} {target}" + (f" <- {action.source}" if action.source else ""))
        except (MountError, OSError) as exc:
            failures += 1
            emit(f"ERROR   {target}: {exc}")
            # A state-write failure must not be followed by further mutations.
            if isinstance(exc, OSError):
                break
    return failures


def main(argv=None):
    parser = argparse.ArgumentParser(description="手动同步原生 mount --bind 挂载；普通用户运行，挂载操作通过 sudo 提权。")
    parser.add_argument("command", choices=["sync"])
    parser.add_argument("config", nargs="?", default="mounts.json", help="默认读取当前目录的 mounts.json")
    parser.add_argument("--dry-run", action="store_true", help="只显示计划，不创建目录、状态或挂载")
    parser.add_argument("--state-dir", type=Path, help="覆盖配置文件旁的 .mountctl 状态目录，仍按机器/用户/启动/命名空间隔离")
    args = parser.parse_args(argv)
    try:
        config = Path(args.config).expanduser().resolve(strict=True)
        desired = load_manifest(config)
        store = StateStore(config, args.state_dir)
        store.check_targets(desired)
        backend = SystemBackend()
        if args.dry_run:
            records = store.load()
            store.check_targets(records)
            actions = make_plan(desired, records, backend.mounts())
            for action in actions:
                print(f"{action.kind.upper():7} {action.target}" + (f" <- {action.source}" if action.source else ""))
            print(f"预览完成，未作修改。状态文件: {store.path}")
            return 0
        with store.lock():
            records = store.load()
            store.check_targets(records)
            actions = make_plan(desired, records, backend.mounts())
            backend.preflight(actions)
            # Also persist a rename/legacy migration when all mounts are KEEP.
            store.save(records)
            failures = execute_plan(actions, records, store, backend)
        print(f"同步完成，失败 {failures} 项。状态文件: {store.path}")
        return 1 if failures else 0
    except (MountError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断；再次执行同一命令可核对并恢复已记录的操作。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
