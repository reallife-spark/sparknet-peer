"""Relocation and write-safety regressions.

Two properties that are easy to break and expensive to notice late:

1. No tracked code or systemd unit may hardcode the vault's absolute path.
2. scripts/sparknet-install-config.sh must have NO write primitive at all.

The second is asserted with a strict token scan rather than an allow-list, so
the script can never quietly grow an `--apply` flag. The one exemption is
redirecting to /dev/null, which mutates nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

VAULT_ROOT = Path(__file__).resolve().parent.parent

# Paths that must not contain a hardcoded vault location.
SCANNED_GLOBS = ("scripts/*.sh", "systemd/*.service", "systemd/*.timer", "systemd/*.sh", "services/*.py")
SCANNED_FILES = ("Makefile", "pyproject.toml")

HARDCODED = "/mnt/knowledge/sparknet"


def _targets() -> list[Path]:
    found: list[Path] = []
    for pattern in SCANNED_GLOBS:
        found.extend(sorted(VAULT_ROOT.glob(pattern)))
    for name in SCANNED_FILES:
        path = VAULT_ROOT / name
        if path.exists():
            found.append(path)
    return found


def test_no_hardcoded_vault_path() -> None:
    """Any absolute vault path in code is a relocation bug."""
    offenders: list[str] = []
    for path in _targets():
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if HARDCODED in line:
                offenders.append(f"{path.relative_to(VAULT_ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, "hardcoded vault path(s) found:\n" + "\n".join(offenders)


def test_systemd_units_have_no_absolute_vault_path() -> None:
    units = sorted(VAULT_ROOT.glob("systemd/*.service")) + sorted(VAULT_ROOT.glob("systemd/*.timer"))
    assert units, "no systemd units found — did they move?"
    for unit in units:
        text = unit.read_text()
        assert "WorkingDirectory=" not in text, f"{unit.name} pins WorkingDirectory"
        for lineno, line in enumerate(text.splitlines(), start=1):
            if line.strip().startswith(("ExecStart=", "ExecStartPre=", "Environment=")):
                assert "/mnt/knowledge" not in line, f"{unit.name}:{lineno} pins a machine path: {line.strip()}"


#: Where the installer writes shims, and which units may reference each.
USER_SHIM_DIR = "%h/.local/bin"
SYSTEM_SHIM_DIR = "/usr/local/libexec/sparknet"


def _shim_names_generated_by_installer() -> tuple[set[str], set[str]]:
    """Parse the two shim lists out of the installer.

    Deriving this from the installer (rather than restating it) means the test
    fails if a unit references a shim nobody generates.
    """
    text = (VAULT_ROOT / "scripts" / "sparknet-install-units.sh").read_text()
    user = set(re.search(r"for name in ([^;]+); do\n  write_shim \"\$BIN_DIR\"", text).group(1).split())
    system = set(re.search(r"ROOT_UNITS=\(([^)]+)\)", text).group(1).split())
    return user, system


def test_units_invoke_generated_shims() -> None:
    """Every ExecStart must call a shim the installer actually generates.

    Two destinations, and which one a unit may use depends on its manager:
      * user units run under the user manager, where %h is the user's home.
      * system units run under the system manager as root, where %h is
        /root/.local/bin — a directory the user's install never creates. A
        User=root unit pointing there starts and fails.
    """
    user_shims, system_shims = _shim_names_generated_by_installer()
    generated = {f"sparknet-{n}" for n in user_shims | system_shims}

    for unit in sorted(VAULT_ROOT.glob("systemd/*.service")):
        text = unit.read_text()
        is_system_unit = "WantedBy=multi-user.target" in text
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("ExecStart="):
                continue
            assert "sparknet-" in stripped, f"{unit.name} does not use a generated shim: {stripped}"
            # The executable is the token right after "ExecStart="; later tokens
            # are arguments (--dry-run, paths, ...).
            executable = stripped[len("ExecStart=") :].split()[0]
            shim = executable.split("/")[-1]
            assert shim in generated, f"{unit.name} calls {shim}, which the installer never generates"

            if is_system_unit:
                assert "%h" not in stripped, (
                    f"{unit.name} is a system unit but uses %h, which resolves to root's home: {stripped}"
                )
                assert SYSTEM_SHIM_DIR in stripped, f"{unit.name} should call {SYSTEM_SHIM_DIR}: {stripped}"
                assert shim.removeprefix("sparknet-") in system_shims, (
                    f"{unit.name} needs a system shim, but sparknet-{shim.removeprefix('sparknet-')} "
                    f"is only in the user list"
                )
            else:
                assert USER_SHIM_DIR in stripped, f"{unit.name} should call {USER_SHIM_DIR}: {stripped}"


def test_system_units_do_not_depend_on_the_invoking_users_home() -> None:
    """A system unit must not read state from an operator's $HOME."""
    for unit in sorted(VAULT_ROOT.glob("systemd/*.service")):
        text = unit.read_text()
        if "WantedBy=multi-user.target" not in text:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("ExecStart=", "EnvironmentFile=")):
                assert "$HOME" not in stripped, f"{unit.name} references $HOME: {stripped}"


def _strip_harmless(text: str) -> str:
    """Remove /dev/null redirects and fd merges, which mutate nothing."""
    text = re.sub(r"\d?>>?\s*/dev/null", " ", text)
    text = re.sub(r"\b\d?>&\d\b", " ", text)
    return text


# Write primitives that must not appear in the dry-run installer.
WRITE_PRIMITIVES = (
    "cp ",
    "install ",
    "tee",
    "mv ",
    "rsync",
    "dd ",
    "sudo",
    "netplan apply",
    "--apply",
    "-o ",
)


def test_install_config_has_no_write_primitive() -> None:
    script = VAULT_ROOT / "scripts" / "sparknet-install-config.sh"
    assert script.exists()
    sanitized = _strip_harmless(script.read_text())

    offenders: list[str] = []
    for lineno, line in enumerate(sanitized.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for token in WRITE_PRIMITIVES:
            if token in stripped:
                offenders.append(f"{script.name}:{lineno}: found {token!r} in {stripped!r}")
    assert not offenders, "install-config.sh must never write:\n" + "\n".join(offenders)


def test_install_config_accepts_no_apply_flag() -> None:
    """It takes no arguments that could enable writing."""
    text = _strip_harmless((VAULT_ROOT / "scripts" / "sparknet-install-config.sh").read_text())
    assert "for arg in" not in text, "install-config.sh should not parse flags at all"
    assert "case $arg" not in text


def test_install_units_writes_only_outside_the_vault() -> None:
    """install-units.sh does write, but only to ~/.local/bin and systemd dirs."""
    script = VAULT_ROOT / "scripts" / "sparknet-install-units.sh"
    sanitized = _strip_harmless(script.read_text())
    # No destination may be inside the repository.
    assert '> "$target"' in sanitized, "expected the shim write to target the generated file"
    for line in sanitized.splitlines():
        if re.search(r'>\s*"?\$', line):
            assert "/etc/systemd" not in line, (
                f"unit write must go through the split dirs, not a raw redirect: {line.strip()}"
            )


def test_template_tree_is_gone() -> None:
    """The duplicate template/ copy must not come back."""
    assert not (VAULT_ROOT / "template").exists(), "template/ was removed; do not reintroduce it"


def test_gitignore_covers_vault_local_state() -> None:
    ignored = (VAULT_ROOT / ".gitignore").read_text()
    for entry in (".state/", ".cache/", ".venv/"):
        assert entry in ignored, f".gitignore must ignore {entry}"
