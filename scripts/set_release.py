"""Point this repository at a specific databricks-emulator release.

Sail and spark-agent ship on fabric-emulator's cadence. Only the workspace
binary moves with a databricks-emulator tag.
"""

from __future__ import annotations

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import digests  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
VERSIONS = ROOT / "versions.env"
PYPROJECT = ROOT / "pyproject.toml"
TRACKS_THE_RELEASE = ("DATABRICKS_EMULATOR_VERSION",)
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?$")

# The release tag inside the databricks-target wheel URL in pyproject.toml.
# The image pin and the Python client must come from the SAME release: a
# workspace binary and a client that disagree about the contract is the one
# mismatch this repository exists to notice, not to ship.
WHEEL_TAG = re.compile(
    r"(databricks-emulator/releases/download/v)\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?(/)"
)


def set_version(text: str, version: str) -> tuple[str, dict[str, str]]:
    moved = {}
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, old = stripped.partition("=")
        key, old = key.strip(), old.strip()
        if key in TRACKS_THE_RELEASE:
            moved[key] = old
            lines[i] = f"{key}={version}\n"
    return "".join(lines), moved


def set_wheel(text: str, version: str) -> tuple[str, int]:
    """Point the databricks-target wheel URL at release `version`.

    Only the tag moves. The wheel's own version is the package's, not the
    emulator's, and the two are deliberately unrelated: a release can ship an
    unchanged client.
    """
    new, n = WHEEL_TAG.subn(rf"\g<1>{version}\g<2>", text)
    return new, n


def main() -> int:
    if len(sys.argv) != 2:
        sys.exit("usage: set_release.py <version>   e.g. set_release.py 0.2.0")
    version = sys.argv[1].lstrip("v")
    if not SEMVER.match(version):
        sys.exit(f"not a version: {version!r} — expected something like 0.2.0")
    text = VERSIONS.read_text(encoding="utf-8")
    new, moved = set_version(text, version)
    missing = [k for k in TRACKS_THE_RELEASE if k not in moved]
    if missing:
        sys.exit(f"{VERSIONS.name} has no {', '.join(missing)} to set")
    # THE DIGEST MOVES WITH THE TAG. Docker ignores the tag in
    # `repo:tag@sha256:...` and fetches the digest, so writing a new version
    # beside the old digest would pull the PREVIOUS image while the run reports
    # the new release — a release test for a release nobody ran. Resolved
    # BEFORE the write, so a tag that is not published yet fails here rather
    # than leaving versions.env naming images that do not exist.
    new, digest_before = digests.rewrite(
        new, "DATABRICKS_EMULATOR",
        digests.digest_of(digests.PINS["DATABRICKS_EMULATOR"][0], version))

    VERSIONS.write_text(new, encoding="utf-8")
    for key, old in moved.items():
        note = "  (unchanged)" if old == version else ""
        print(f"  {key}: {old} -> {version}{note}")
    after = digests.value(new, "DATABRICKS_EMULATOR_DIGEST")
    print(f"  DATABRICKS_EMULATOR_DIGEST: {digest_before[:19]}… -> {after[:19]}…"
          f"{'  (unchanged)' if digest_before == after else ''}")

    # The Python client comes from the same release as the image.
    proj = PYPROJECT.read_text(encoding="utf-8")
    moved_wheel, n = set_wheel(proj, version)
    if n == 0:
        sys.exit(
            f"{PYPROJECT.name} has no databricks-target wheel URL to move. "
            f"If it went back to a path source, this script and "
            f"test_the_target_wheel_matches_the_pinned_release both need a look."
        )
    PYPROJECT.write_text(moved_wheel, encoding="utf-8")
    print(f"  databricks-target wheel: -> v{version}")
    print("  run `uv lock` to refresh the lockfile")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
