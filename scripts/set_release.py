"""Point versions.env at a release. TWO cadences, because this cell has two.

`DATABRICKS_EMULATOR_VERSION` and the databricks-target wheel move with a
databricks-emulator release. `SAIL_ENGINE_RELEASE` and `SPARK_CLIENT_RELEASE`
do not: Sail and the statement agent are built and published by
**fabric-emulator**, so what a fabric release moves is their digest and the
label recording which release built them.

    python3 scripts/set_release.py --databricks 0.2.9
    python3 scripts/set_release.py --fabric 0.35.0

THE FABRIC CADENCE HAD NO ARGUMENT AT ALL until 0.35.0. A bare version was
read as a databricks one, so `set_release.py 0.35.0` answered

    cannot read digest for ghcr.io/calvinchengx/databricks-emulator:0.35.0: not found

and the fabric half of this cell's pins had to be hand-edited during every
fabric sweep. Hand-editing is what leaves a _RELEASE label naming last month's
release above this month's digest, which is the drift this script exists to
prevent. databricks-platform-airflow3 grew the same two flags for the same
reason; this is that idea, using THIS repository's `digests` module rather
than a copy of the sibling's script, which has no databricks-target wheel to
move and no digests module to reuse.
"""

from __future__ import annotations

import pathlib
import re
import sys
import urllib.request

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


# Which images a fabric release moves here, and the pin in fabric-emulator's
# pyproject.toml each is tagged with. The same map as fabric-emulator's
# scripts/image_tags.py.
TAGGED_BY = {"SAIL_ENGINE": "pysail", "SPARK_CLIENT": "pyspark-client"}
CARRIES_A_FABRIC_RELEASE = tuple(TAGGED_BY)

# A TAG, not a branch: what the release was built from, and it cannot move.
FABRIC_PYPROJECT = ("https://raw.githubusercontent.com/calvinchengx/"
                    "fabric-emulator/v{release}/pyproject.toml")


def fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read().decode("utf-8")


def carried_versions(release: str) -> dict[str, str]:
    """The dependency version each sidecar carries in fabric `release`.

    Read with image_tags.py's own rule: exactly one `==` pin per package.
    """
    url = FABRIC_PYPROJECT.format(release=release)
    try:
        text = fetch(url)
    except OSError as err:
        raise SystemExit(f"cannot read {url}: {err}") from None
    carried = {}
    for prefix, package in TAGGED_BY.items():
        found = set(re.findall(rf'"{re.escape(package)}==([0-9][^"]*)"', text))
        if len(found) != 1:
            raise SystemExit(f"v{release} pins {package} as {sorted(found) or 'nothing'}; "
                             f"expected exactly one == version")
        carried[prefix] = found.pop()
    return carried


def set_fabric(version: str) -> int:
    """Move the fabric-built sidecars: digest, release label and _VERSION.

    The digest comes from the release's own tag. _VERSION names the dependency
    each image carries (pysail, pyspark-client), so it never takes the release
    number; it moves to whatever that release ships. This used to hold it
    still, and v0.36.0 moved pysail 0.7.0 -> 0.7.1.
    """
    carried = carried_versions(version)
    text = VERSIONS.read_text(encoding="utf-8")
    for prefix in CARRIES_A_FABRIC_RELEASE:
        image = digests.PINS[prefix][0]
        digest = digests.digest_of(image, version)
        text, before = digests.rewrite(text, prefix, digest)
        text, release_before = digests.rewrite_release(text, prefix, version)
        after = digests.value(text, f"{prefix}_DIGEST")
        print(f"  {image}:{version} -> {digest[:19]}…")
        print(f"    {prefix}_DIGEST: {before[:19]}… -> {after[:19]}…"
              f"{'  (unchanged)' if before == after else ''}")
        print(f"    {prefix}_RELEASE: {release_before} -> {version}"
              f"{'  (unchanged)' if release_before == version else ''}")
        dep_before = digests.value(text, f"{prefix}_VERSION")
        text = re.sub(rf"^{prefix}_VERSION=.*$", f"{prefix}_VERSION={carried[prefix]}",
                      text, flags=re.M)
        print(f"    {prefix}_VERSION: {dep_before} -> {carried[prefix]}"
              f"{'  (unchanged)' if dep_before == carried[prefix] else ''}")
    VERSIONS.write_text(text, encoding="utf-8")
    print("  DATABRICKS_EMULATOR_VERSION is NOT moved: it ships on the "
          "databricks-emulator cadence. Use --databricks for that.")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if len(argv) == 2 and argv[0] in ("--databricks", "--fabric"):
        cadence, raw = argv
    elif len(argv) == 1 and not argv[0].startswith("-"):
        # THE BARE FORM STILL MEANS DATABRICKS, so every existing caller and
        # every existing test keeps working. Only the new cadence needs a flag.
        cadence, raw = "--databricks", argv[0]
    else:
        sys.exit("usage: set_release.py [--databricks|--fabric] <version>\n"
                 "  --databricks 0.2.9   the workspace binary and its client\n"
                 "  --fabric 0.35.0      Sail and the statement agent\n"
                 "  a bare version means --databricks")
    version = raw.lstrip("v")
    if not SEMVER.match(version):
        sys.exit(f"not a version: {version!r} — expected something like 0.2.0")
    if cadence == "--fabric":
        return set_fabric(version)
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
