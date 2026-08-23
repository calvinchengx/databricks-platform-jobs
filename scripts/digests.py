"""Which images this stack fetches by digest, and how to resolve one.

SHARED so the two callers cannot disagree. `set_release.py` moves the digest of
the image a databricks-emulator release retags; `refresh_digests.py` re-resolves
all of them for a human bumping a version by hand. If each kept its own list,
the one that was not edited would leave a digest behind — and a digest left
behind is not a stale pin, it is the WRONG IMAGE running silently, because
docker ignores the tag in `repo:tag@sha256:...`.
"""
import re
import subprocess

# var prefix -> the image its _VERSION tags
PINS = {
    "DATABRICKS_EMULATOR": "ghcr.io/calvinchengx/databricks-emulator",
    "SAIL": "ghcr.io/calvinchengx/emulator-sail",
    "SPARK_AGENT": "ghcr.io/calvinchengx/emulator-spark-agent",
}


def digest_of(image: str, tag: str) -> str:
    """The INDEX digest the tag points at right now.

    The index, not a platform's manifest: pinning `linux/amd64` would give a
    stack that pulls on CI and fails on an arm64 laptop, which is a worse bug
    than the one being fixed because it only appears off the CI runner.
    """
    out = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", f"{image}:{tag}",
         "--format", "{{.Manifest.Digest}}"],
        capture_output=True, text=True)
    if out.returncode != 0 or not out.stdout.strip().startswith("sha256:"):
        raise SystemExit(f"cannot read digest for {image}:{tag}: "
                         f"{(out.stderr or out.stdout).strip()[:200]}")
    return out.stdout.strip()


def value(text: str, var: str) -> str:
    found = re.search(rf"^{var}=(.+)$", text, re.M)
    if not found:
        raise SystemExit(f"{var} not found in versions.env")
    return found.group(1).strip()


def rewrite(text: str, prefix: str, digest: str) -> tuple[str, str]:
    """Set one _DIGEST, returning the new text and what it was."""
    before = value(text, f"{prefix}_DIGEST")
    return re.sub(rf"^{prefix}_DIGEST=.*$", f"{prefix}_DIGEST={digest}",
                  text, flags=re.M), before
