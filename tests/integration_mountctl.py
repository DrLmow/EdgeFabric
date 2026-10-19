"""Opt-in real FUSE integration test. Run as a normal user on the host."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import mountctl  # noqa: E402


def main():
    base = Path(tempfile.mkdtemp(prefix="edgefabric-mountctl-integration-"))
    project = base / "project"
    project.mkdir()
    source = base / "sdk"
    source.mkdir()
    (source / "version").write_text("one")
    other = base / "sdk two"
    other.mkdir()
    (other / "version").write_text("two")
    config = project / "mounts.json"
    target = project / "sdk view"
    state = base / "state"
    tool = Path(__file__).resolve().parents[1] / "tools/mountctl.py"
    backend = mountctl.SystemBackend()
    busy = None

    def manifest(src):
        entries = [] if src is None else [{"source": str(src), "target": "sdk view"}]
        config.write_text(json.dumps({"version": 1, "mounts": entries}))

    def sync(*args, expected=0):
        result = subprocess.run(
            [sys.executable, "-B", str(tool), "sync", str(config), "--state-dir", str(state), *args],
            capture_output=True, text=True,
        )
        print(result.stdout, end="")
        print(result.stderr, end="")
        assert result.returncode == expected, (result.returncode, expected)

    try:
        manifest(source)
        sync("--dry-run")
        assert not target.exists() and not state.exists()
        sync()
        assert (target / "version").read_text() == "one"
        assert (target / "version").resolve() == target / "version"
        (target / "written-by-user").write_text("shared content")
        assert (source / "written-by-user").read_text() == "shared content"
        before = backend.mounts()[str(target)][0]["id"]
        # Simulate interruption after FUSE mounted but before identity was saved.
        store = mountctl.StateStore(config, state)
        with store.lock():
            records = store.load()
            records[str(target)]["mount"] = None
            store.save(records)
        sync()
        assert backend.mounts()[str(target)][0]["id"] == before
        assert store.load()[str(target)]["mount"] is not None
        manifest(other)
        sync()
        assert (target / "version").read_text() == "two"
        busy = subprocess.Popen(["/bin/sleep", "60"], cwd=target)
        manifest(None)
        sync(expected=1)
        assert str(target) in backend.mounts()
        assert str(target) in store.load()
        busy.terminate()
        busy.wait()
        busy = None
        sync()
        assert str(target) not in backend.mounts()
        assert not store.load()
        assert target.is_dir() and not list(target.iterdir())
        assert (source / "written-by-user").read_text() == "shared content"
        assert (other / "version").read_text() == "two"
        print("PASS: real FUSE preview, read/write, path preservation, interruption recovery, "
              "idempotence, replacement, busy retry, and removal")
    finally:
        if busy is not None:
            busy.terminate()
            busy.wait()
        entries = backend.mounts().get(str(target), [])
        if len(entries) == 1 and entries[0]["fstype"].startswith("fuse." + mountctl.TOKEN_PREFIX):
            subprocess.run([backend.unmounter(), "-u", "--", str(target)], check=False)
        if any(Path(p) == base or base in Path(p).parents for p in backend.mounts()):
            print("Fixture retained because a mount remains:", base, file=sys.stderr)
        else:
            shutil.rmtree(base)


if __name__ == "__main__":
    main()
