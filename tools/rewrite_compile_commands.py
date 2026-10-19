#!/usr/bin/env python3
"""Map selected paths in a compilation database to a symlinked source tree."""

import argparse
import json
import os
from pathlib import Path
import shlex
import stat
import sys
import tempfile


HEADER_PATH_OPTIONS = ("-I", "-isystem", "-iquote", "-idirafter", "-include", "-imacros")
PREFIX_MAP_OPTIONS = ("-fmacro-prefix-map", "-ffile-prefix-map", "-fdebug-prefix-map")


def replace_prefix(path, old, new):
    """Replace only a complete leading path component; do not resolve symlinks."""
    if old == "/":
        if not path.startswith("/"):
            return path
        suffix = path[1:]
        return new.rstrip("/") + "/" + suffix if suffix else new
    if path == old:
        return new
    if path.startswith(old + "/"):
        suffix = path[len(old):]
        return new.rstrip("/") + suffix
    return path


def command_tokens(command):
    """Yield (start, end, argv value) while retaining the original shell text."""
    index = 0
    while index < len(command):
        while index < len(command) and command[index].isspace():
            index += 1
        if index == len(command):
            break
        start = index
        quote = None
        while index < len(command):
            char = command[index]
            if quote is None and char.isspace():
                break
            if char == "'" and quote != '"':
                quote = None if quote == "'" else "'"
            elif char == '"' and quote != "'":
                quote = None if quote == '"' else '"'
            elif char == "\\" and quote != "'":
                index += 1
                if index == len(command):
                    raise ValueError("command ends with an escape character")
            index += 1
        if quote is not None:
            raise ValueError("command contains an unclosed quote")
        raw = command[start:index]
        values = shlex.split(raw, posix=True)
        if len(values) != 1:
            raise ValueError(f"cannot parse command token: {raw!r}")
        yield start, index, values[0]


def rewrite_arguments(arguments, source, old, new):
    rewritten = []
    previous = None
    for argument in arguments:
        if not isinstance(argument, str):
            raise ValueError("arguments must contain only strings")
        updated = argument
        if previous in HEADER_PATH_OPTIONS or previous == "-c":
            updated = replace_prefix(argument, old, new)
        elif argument == source:
            updated = replace_prefix(argument, old, new)
        else:
            for option in HEADER_PATH_OPTIONS:
                if argument.startswith(option) and len(argument) > len(option):
                    updated = option + replace_prefix(argument[len(option):], old, new)
                    break
            else:
                for option in PREFIX_MAP_OPTIONS:
                    prefix = option + "="
                    if argument.startswith(prefix):
                        left, separator, right = argument[len(prefix):].partition("=")
                        if separator:
                            updated = prefix + replace_prefix(left, old, new) + separator + right
                        break
        rewritten.append(updated)
        previous = argument
    return rewritten


def rewrite_command(command, source, old, new):
    tokens = list(command_tokens(command))
    arguments = [value for _, _, value in tokens]
    rewritten = rewrite_arguments(arguments, source, old, new)
    pieces = []
    last = 0
    for (start, end, original), updated in zip(tokens, rewritten):
        pieces.append(command[last:start])
        pieces.append(command[start:end] if updated == original else shlex.quote(updated))
        last = end
    pieces.append(command[last:])
    return "".join(pieces)


def rewrite_database(entries, old, new):
    if not isinstance(entries, list):
        raise ValueError("compilation database must be a JSON array")
    changed = 0
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"entry {index} must be an object")
        try:
            source = entry["file"]
            directory = entry["directory"]
        except KeyError as error:
            raise ValueError(f"entry {index} is missing {error.args[0]!r}") from error
        if not isinstance(source, str) or not isinstance(directory, str):
            raise ValueError(f"entry {index}: file and directory must be strings")
        updated = dict(entry)
        updated["file"] = replace_prefix(source, old, new)
        updated["directory"] = replace_prefix(directory, old, new)
        if "command" in entry:
            if not isinstance(entry["command"], str):
                raise ValueError(f"entry {index}: command must be a string")
            updated["command"] = rewrite_command(entry["command"], source, old, new)
        if "arguments" in entry:
            if not isinstance(entry["arguments"], list):
                raise ValueError(f"entry {index}: arguments must be an array")
            updated["arguments"] = rewrite_arguments(entry["arguments"], source, old, new)
        if updated != entry:
            entries[index] = updated
            changed += 1
    return changed


def is_compilation_database(entries):
    """Recognize compilation databases when scanning a mixed JSON directory."""
    return (isinstance(entries, list) and bool(entries) and
            all(isinstance(entry, dict) and
                isinstance(entry.get("directory"), str) and
                isinstance(entry.get("file"), str) and
                ("command" in entry or "arguments" in entry)
                for entry in entries))


def write_database(destination, entries, mode_source):
    destination.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE((destination if destination.exists() else mode_source).stat().st_mode)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=destination.parent,
                                         prefix=f".{destination.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(entries, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.chmod(mode)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path, help="JSON file or directory to scan recursively")
    parser.add_argument("old_prefix", help="original path prefix (A); relative to current directory")
    parser.add_argument("new_prefix", help="symlinked path prefix (B); relative to current directory")
    parser.add_argument("--output", type=Path,
                        help="output file, or output directory when the input is a directory")
    args = parser.parse_args()

    # abspath normalizes relative paths without following B's symlink.
    old = os.path.abspath(os.path.expanduser(args.old_prefix))
    new = os.path.abspath(os.path.expanduser(args.new_prefix))
    if not os.path.isdir(new):
        parser.error(f"new prefix is not an existing directory: {new}")
    if os.path.exists(old) and not os.path.samefile(old, new):
        parser.error(
            f"A resolves to {os.path.realpath(old)}, but B resolves to "
            f"{os.path.realpath(new)}; choose B's resolved directory as A"
        )

    source = args.database
    if source.is_file():
        files = [source]
        scanning = False
    elif source.is_dir():
        scanning = True
        files = sorted(source.rglob("*.json"))
        if args.output:
            output_root = args.output.absolute()
            source_root = source.absolute()
            if output_root != source_root and output_root.is_relative_to(source_root):
                files = [path for path in files if not path.absolute().is_relative_to(output_root)]
        if not files:
            parser.error(f"no JSON files found under {source}")
    else:
        parser.error(f"input is neither a file nor a directory: {source}")

    if args.output and scanning and args.output.exists() and not args.output.is_dir():
        parser.error("--output must be a directory when the input is a directory")
    if args.output and not scanning and args.output.is_dir():
        parser.error("--output must be a file when the input is a file")

    databases = skipped = changed_files = changed_entries = 0
    errors = []
    for path in files:
        try:
            with path.open(encoding="utf-8") as stream:
                entries = json.load(stream)
            if scanning and not is_compilation_database(entries) and not (
                path.name == "compile_commands.json" and entries == []
            ):
                if path.name == "compile_commands.json":
                    raise ValueError("file is not a compilation database")
                skipped += 1
                continue
            changed = rewrite_database(entries, old, new)
            databases += 1
            if args.output or changed:
                destination = (args.output / path.relative_to(source) if scanning and args.output
                               else args.output or path)
                write_database(destination, entries, path)
            if changed:
                changed_files += 1
                changed_entries += changed
                print(f"Updated {changed} of {len(entries)} entries: {path}")
        except (OSError, ValueError) as error:
            errors.append(f"{path}: {error}")

    if scanning:
        print(f"Scanned {len(files)} JSON files: {databases} compilation databases, "
              f"{skipped} other JSON files; updated {changed_entries} entries in "
              f"{changed_files} compilation databases")
    elif not errors and not changed_files:
        print(f"Updated 0 of {len(entries)} entries: {source}")
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    if errors:
        return 1
    if scanning and not databases:
        print(f"error: no compilation databases found under {source}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
