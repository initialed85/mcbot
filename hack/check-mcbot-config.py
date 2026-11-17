"""Check a deployment's ConfigMap and Secret against the configuration this build asks for.

The gap this closes is not "the app is broken". It is "the app was fine and then somebody renamed a variable, or
added a typo, or upgraded mcbot to a build that wants a variable this deployment never supplied" - and each of
those produces a *running* deployment with a setting that silently took a default. Half of them default to
something safe, which is the point: a deployment that quietly stops being able to do the thing you deployed it
for looks identical to a deployment that is working.

Reads plain Kubernetes manifests (no cluster access, no YAML library) and .env files, and reports four things:

- a required variable that is absent;
- a variable the deployment asked for by switching something on, and did not supply;
- a name the deployment sets that no version of this code reads - a typo, a leftover, or a rename that went one
  way only;
- what the deployment actually arms: the capability switches and the plugin allowlist, as resolved values, so a
  reviewer can see "in-game actions off, no plugin allowed" in the output instead of trusting the file.

    python3 hack/check-mcbot-config.py deploy/example/configmap.yaml deploy/example/secret.example.yaml
    python3 hack/check-mcbot-config.py deploy/example/configmap.yaml deploy/example/secret.example.yaml

Exit 0 when the deployment is complete, 1 when it is not.
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config_table as cfg  # noqa: E402

KEY_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+):\s*(.*)$")


def read_manifest(path: str) -> dict[str, str]:
    """Keys from `data:` / `stringData:` blocks of a ConfigMap or Secret, or from a .env file.

    Line-based on purpose. This runs in CI, in a pod, and on a laptop; none of those have PyYAML in common, and
    the only thing needed here is which names carry which values. Multi-line block scalars are not parsed - a
    ConfigMap value for mcbot is a single line in every case this checker knows about.
    """
    values: dict[str, str] = {}
    inside = False
    indent = 0
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if re.match(r"^(data|stringData|env):", line):
                inside = True
                indent = len(line) - len(line.lstrip())
                continue
            if not inside:
                # A flat KEY=value file.
                m = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", stripped)
                if m:
                    values[m.group(1)] = m.group(2).strip().strip('"').strip("'")
                continue
            current_indent = len(line) - len(line.lstrip())
            if current_indent <= indent and stripped and not stripped.startswith("-"):
                inside = False
                continue
            if stripped.startswith("- "):  # a `env:` list entry, e.g. name:/value: pairs
                m = re.match(r"^- name: ([A-Za-z0-9_.-]+)$", stripped)
                if m:
                    values.setdefault(m.group(1), "")
                continue
            if stripped.startswith("name:"):
                m = KEY_RE.match(stripped)
                if m:
                    values.setdefault(m.group(1), "")
                continue
            m = KEY_RE.match(stripped)
            if m:
                values[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return values


def check(values: dict[str, str], origin: str, complete: dict[str, str]) -> tuple[list[str], list[str]]:
    """`values` is one file; `complete` is every file in the deployment merged.

    Required-ness is judged against the merged view because a Secret is allowed to be where the password lives.
    An unknown name is judged per file, because "which file has the typo" is the useful question.
    """
    problems: list[str] = []
    notes: list[str] = []

    missing = cfg.missing_required(complete)
    conditional = cfg.missing_conditions(complete)
    unknown = sorted(name for name in values
                     if name not in cfg.names()
                     and re.match(r"^(MCBOT_|DISCORD_|PI_)", name))

    if missing:
        problems.append(f"{origin}: required and absent: {', '.join(missing)}")
    if conditional:
        problems.append(f"{origin}: required by the settings in force: "
                        + "; ".join(f"{name} ({why})" for name, why in conditional))
    if unknown:
        problems.append(f"{origin}: set but not read by this build (a typo, a leftover, or a rename that went "
                        f"one way only): {', '.join(unknown)}")

    for name in sorted(values):
        if name in cfg.names() and not str(values[name]).strip():
            e = cfg.entry(name)
            if e.required or e.when:
                problems.append(f"{origin}: {name} is present but empty, and it has no default to fall back to")
            elif e.default:
                notes.append(f"{origin}: {name} is empty, so this build uses its default {e.default!r}")

    return problems, notes


def armed(values: dict[str, str]) -> list[str]:
    """What the deployment arms, as resolved values rather than as strings in a file.

    Printed whether or not anything is wrong, because the interesting question about a bot that can change a
    server is not whether it starts. These four lines are also the fastest way to see that an upgrade changed a
    default underneath you.
    """
    out = []
    for name, what in (
        ("MCBOT_SERVER_ACTIONS", "out-of-game changes (manifests, whitelist, restarts)"),
        ("MCBOT_INGAME_ACTIONS", "in-game changes (give, teleport, gamemode, kick)"),
    ):
        on = cfg.value_of(name, values)
        out.append(f"{name} -> {'ON' if on else 'off'} ({what})")
    allow = cfg.value_of("MCBOT_PLUGIN_ALLOWLIST", values)
    out.append("MCBOT_PLUGIN_ALLOWLIST -> "
               + (", ".join(allow) if allow else "empty (no plugin jar may be committed)"))
    out.append(f"MCBOT_APPROVER_MODE -> {cfg.value_of('MCBOT_APPROVER_MODE', values)}")
    return out


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    all_problems: list[str] = []
    all_notes: list[str] = []
    merged: dict[str, str] = {}
    for path in argv:
        if not os.path.exists(path):
            print(f"FAIL: no such file: {path}")
            return 1
        values = read_manifest(path)
        if not values:
            print(f"FAIL: {path}: no configuration keys found")
            return 1
        merged.update(values)

    # Two passes: each file on its own for names nothing reads, the merged deployment for what is required.
    for path in argv:
        values = read_manifest(path)
        problems, notes = check(values, os.path.basename(path), merged)
        if not values:
            continue
        all_problems += problems
        all_notes += notes

    for line in all_notes:
        print("  " + line)
    for line in armed(merged):
        print("  armed: " + line)
    for line in all_problems:
        print("PROBLEM " + line)
    if all_problems:
        print(f"\nFAIL: {len(all_problems)} configuration problem(s); see docs/configuration.md")
        return 1
    print(f"\nOK: {len(merged)} variable(s) supplied; every variable this build requires is present")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
