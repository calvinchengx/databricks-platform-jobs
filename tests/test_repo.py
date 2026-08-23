"""Repo-boundary tests. No Docker, no emulator."""

from __future__ import annotations

import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_pins_are_immutable():
    pins = {}
    for line in (ROOT / "versions.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            pins[k.strip()] = v.strip()
    assert "DATABRICKS_EMULATOR_VERSION" in pins
    assert "SAIL_VERSION" in pins
    assert "UC_VERSION" in pins
    mutable = {"latest", "stable", "main", "edge"}
    for k, v in pins.items():
        assert v.lower() not in mutable, f"{k}={v}"


def test_compose_reads_every_pin():
    composed = "".join(p.read_text(encoding="utf-8") for p in (ROOT / "compose").glob("*.yml"))
    for line in (ROOT / "versions.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k = line.split("=", 1)[0].strip()
            assert "${" + k in composed, k


def test_makefile_survives_cmd_exe():
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    for bad in (" | ", " && ", " `", " rm "):
        for line in text.splitlines():
            if line.startswith("#") or line.startswith("ifeq") or line.startswith("  SHELL"):
                continue
            if ":" in line and not line.startswith("\t") and not line.startswith(" "):
                continue
            if line.startswith("\t"):
                assert bad not in line, f"cmd.exe-unsafe recipe: {line!r}"


def test_the_target_wheel_matches_the_pinned_release():
    """The client wheel and the image come from the SAME release.

    `databricks-target` is installed from a published wheel rather than from
    the emulator's source tree, which is what makes this repository a consumer:
    it builds from what a release ships, so anything that works here works for
    anyone with the same release.

    That puts the version in two files, and a copied version with nothing
    checking it is a second source of truth that drifts. This is the check.
    A workspace binary and a client that disagree about the contract is the one
    mismatch this repository exists to notice.
    """
    pins = {}
    for line in (ROOT / "versions.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            pins[k.strip()] = v.strip()
    version = pins["DATABRICKS_EMULATOR_VERSION"]

    proj = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "databricks-target = { url =" in proj, (
        "databricks-target must come from the published wheel, not a path: a "
        "consumer that reads the emulator's source tree proves nothing"
    )
    expected = f"databricks-emulator/releases/download/v{version}/"
    assert expected in proj, (
        f"the databricks-target wheel does not come from the pinned release "
        f"v{version}. Run `python scripts/set_release.py {version}`."
    )


def test_no_dependency_comes_from_a_sibling_checkout():
    """This repository must clone and build on its own.

    Both `databricks-target` and `contoso-data-product` install from wheels
    their releases publish. A `path = "../…"` source is invisible to everyone
    who already has the siblings on disk, and fails for everyone who does not,
    which is the whole population this repository claims to serve.

    CI asserts the same thing by checking out one repository and nothing else.
    This test is the fast version, and it names the rule.
    """
    proj = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in proj.splitlines()
        if "path = " in line and "../" in line and not line.lstrip().startswith("#")
    ]
    assert not offenders, (
        "a dependency resolves from a sibling checkout, so a lone clone cannot "
        "build: " + str(offenders)
    )


def test_set_release_moves_only_the_emulator_pin(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "set_release", ROOT / "scripts" / "set_release.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    text = (ROOT / "versions.env").read_text(encoding="utf-8")
    new, moved = mod.set_version(text, "0.2.0")
    assert "DATABRICKS_EMULATOR_VERSION" in moved
    assert "DATABRICKS_EMULATOR_VERSION=0.2.0" in new
    assert "SAIL_VERSION=" in new
    sail = [ln for ln in text.splitlines() if ln.startswith("SAIL_VERSION=")][0]
    assert sail in new


def test_the_vendor_stack_is_generated_from_the_sources_declaration():
    """The vendors are contoso-sources', not this repository's.

    Two platforms' gold numbers are comparable only if the bytes were
    identical, and identical bytes means the same declaration, the same
    fixtures and the same pinned simulator. A vendor block hand-written here
    would be this platform's own data wearing the family's name.
    """
    assert not (ROOT / "compose" / "sources.yml").exists(), (
        "compose/sources.yml is back — vendors belong to contoso-sources, and "
        "a local copy is how the two runtimes quietly stop comparing"
    )
    compose = (ROOT / "scripts" / "compose.py").read_text(encoding="utf-8")
    assert "sources.yaml" in compose and "_data" in compose, (
        "compose.py must generate the vendor fragment from the sources "
        "declaration, and must refuse to start when the fixtures are absent"
    )


def test_the_generator_refuses_a_vendor_kind_it_cannot_run(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "vendor_sources", ROOT / "scripts" / "sources.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    decl = {"vendors": [{"name": "nope", "kind": "telepathy"}]}
    try:
        mod.fragment(decl, str(tmp_path), {"MOKAPI_VERSION": "0.50.0"})
    except SystemExit as exc:
        assert "telepathy" in str(exc)
    else:  # pragma: no cover - the assertion IS the test
        raise AssertionError("an unknown vendor kind was quietly accepted")


def test_vendor_host_ports_do_not_collide_with_the_fabric_platform():
    """Both stacks run on one developer machine, often at the same time.

    A collision does not report itself as a collision: compose fails to bind
    and the message names a port, not the two platforms fighting over it. The
    Fabric platform owns 180xx / 19092 / 55432; this one owns 181xx / 19094 /
    55434.
    """
    text = (ROOT / "scripts" / "sources.py").read_text(encoding="utf-8")
    ns: dict = {}
    for line in text.splitlines():
        if line.startswith(("HOST_BASE", "ERP_DB_HOST_PORT", "ERP_BROKER_HOST_PORT",
                            "ERP_CONNECT_HOST_PORT")):
            k, v = line.split("=", 1)
            ns[k.strip()] = int(v.split("#")[0].strip())
    fabric = {18090, 18091, 18092, 18081, 18082, 18084, 18083, 19092, 55432}
    ours = {ns["HOST_BASE"] + i for i in range(3)} | {
        ns["ERP_DB_HOST_PORT"], ns["ERP_BROKER_HOST_PORT"], ns["ERP_CONNECT_HOST_PORT"]}
    assert not (ours & fabric), f"host ports collide with fabric-platform-notebook-pipelines: {ours & fabric}"


def test_the_locked_wheel_matches_the_pinned_release():
    """The LOCKFILE is what decides which client actually runs.

    test_the_target_wheel_matches_the_pinned_release checks pyproject.toml,
    and that is the declaration. It is not what gets installed: every make
    target runs `uv run --frozen`, and --frozen resolves from uv.lock without
    reading pyproject.toml at all. So a bump that moves versions.env and
    pyproject.toml but not the lock leaves the pin pointing one way and the
    installed client pointing the other, with nothing between them.

    Measured, not hypothesised: with pyproject at v0.2.5 and uv.lock left at
    v0.2.4, `uv run --frozen` installed databricks_target from the v0.2.4
    wheel, reported success, and named the old URL in its direct_url.json.
    That is the new image running against the old client -- the exact
    mismatch this repository exists to notice, arriving silently.
    """
    pins = {}
    for line in (ROOT / "versions.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            pins[k.strip()] = v.strip()
    version = pins["DATABRICKS_EMULATOR_VERSION"]

    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    stale = [
        line.strip()
        for line in lock.splitlines()
        if "databricks-emulator/releases/download/" in line
        and f"/download/v{version}/" not in line
    ]
    assert not stale, (
        f"uv.lock still installs databricks-target from a release other than "
        f"the pinned v{version}. Run `python scripts/set_release.py {version}` "
        f"AND `uv lock` -- the lockfile is what --frozen installs.\n  "
        + "\n  ".join(stale)
    )


def test_the_acceptance_run_adopts_every_file_the_bump_touches():
    """A half-adopted pin publishes a main that fails its own test.

    The adopt step commits what set_release.py changed. set_release.py changes
    versions.env and pyproject.toml, and `uv lock` then changes uv.lock. Commit
    only the first and main carries a pin the other two contradict --
    test_the_target_wheel_matches_the_pinned_release fails on the very commit
    the acceptance run pushed as verified.
    """
    wf = (ROOT / ".github" / "workflows" / "acceptance.yml").read_text(encoding="utf-8")
    adopt = wf[wf.index("Adopt the version this run just verified") :]
    for name in ("versions.env", "pyproject.toml", "uv.lock"):
        assert adopt.count(name) >= 2, (
            f"the adopt step must both TEST and COMMIT {name}; a file left out "
            f"of either half is a pin that main contradicts"
        )
    assert "uv lock" in wf, (
        "the dispatch must refresh the lockfile after set_release.py, or the "
        "run verifies the new image against the client the lock still names"
    )


def test_the_acceptance_run_asserts_the_numbers_and_not_only_the_run():
    """A nightly that proves the pipeline RAN proves nothing about the answer.

    G50: across all seven platforms with an acceptance workflow, none compared a
    snapshot against an expected value. `steps/gold.py` writes
    product_snapshot.json and nothing read it back, so gold could have returned
    different money indefinitely behind a green tick.

    The core checkout must be PINNED. Left tracking main, this cell's
    expectations could move without a reviewed commit here -- a nightly that
    another repository can turn green.
    """
    raw = (ROOT / ".github" / "workflows" / "acceptance.yml").read_text(encoding="utf-8")
    wf = "\n".join(ln for ln in raw.splitlines() if not ln.lstrip().startswith("#"))
    assert "scripts/assert_snapshot.py" in wf, (
        "the acceptance run never asserts the figures core publishes"
    )
    assert "product_snapshot.json" in wf, (
        "the assert step names no snapshot, so it checks nothing"
    )
    core = wf[wf.index("repository: calvinchengx/contoso-data-product\n") :]
    assert re.search(r"ref: [0-9a-f]{40}", core[: core.index("path:")]), (
        "the contoso-data-product checkout is not pinned to a commit"
    )
    assert wf.index("make verify") < wf.index("scripts/assert_snapshot.py"), (
        "the numbers are asserted before the run that produces them"
    )


def test_acceptance_checks_out_every_repository_the_stack_reads():
    """doctor.py and compose.py hard-require a contoso-sources checkout.

    `sources_dir()` resolves `ROOT.parent / "contoso-sources"` unless SOURCES
    overrides it, and both scripts exit rather than guess. The acceptance job
    checked out three repositories and not that one, so `make doctor` failed at
    the first step with "missing the vendor declaration" -- an emulator release
    could not be verified at all, for a reason that had nothing to do with the
    emulator.

    Measured: run 32193426410, the first acceptance run after the vendor
    declaration became load-bearing, died 12 seconds in.

    The declaration alone is not enough either. compose.py checks for the
    materialised bytes under _data/ and says so ("Run `make sources` in ...
    first"), because mokapi serves the exports from that directory; an
    unmaterialised checkout stands up vendors that answer nothing.
    """
    raw = (ROOT / ".github" / "workflows" / "acceptance.yml").read_text(encoding="utf-8")
    # Comments are stripped before anything is looked up. The prose above the job
    # names the targets it is explaining -- `make verify` appears in a comment far
    # above the step that runs it -- so `raw.index` finds the comment and the
    # ordering below fails on a workflow that is correctly ordered. Presence is
    # checked here too: a commented-out checkout must not satisfy this test.
    wf = "\n".join(ln for ln in raw.splitlines() if not ln.lstrip().startswith("#"))
    assert "repository: calvinchengx/contoso-sources" in wf, (
        "acceptance must check out contoso-sources beside this repository, or "
        "doctor.py exits before the emulator is ever started"
    )
    assert "make sources" in wf, (
        "checking out contoso-sources is half of it -- without `make sources` "
        "the exports under _data/ do not exist and compose.py refuses"
    )
    materialise = wf.index("make sources")
    for target in ("make doctor", "make up", "make verify"):
        assert materialise < wf.index(target), (
            f"`make sources` must run before `{target}`; the vendors have to "
            f"exist before anything reads them"
        )


def test_the_platform_holds_no_product():
    """The platform is compose, pins, vendors and scripts. Nothing Contoso.

    This repository used to contain its own product: eighteen step modules --
    ingest, the medallion runners, the target binding -- sitting in `platform/`
    beside the compose files. That made the cell's name a half-truth, and it
    made "a second product can use this platform unchanged" untestable, because
    there was no second thing to point it at.

    The split line is `00-family.md`'s, not this file's invention: a platform
    holds no Contoso name and no product file. `fabric-platform-airflow3`, the
    cell that already got this right, has no `platform/` directory at all —
    it takes PRODUCT as a path and the product carries its own task code.
    """
    assert not (ROOT / "platform").exists(), (
        "a platform/ directory is back — the product's steps belong in the leaf"
    )

    # The Makefile may name the vendors repo (it consumes one) but never a
    # product: `./product` is a mount point, and a default naming Contoso would
    # put the identifier straight back.
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    for line in makefile.splitlines():
        code = line.split("#", 1)[0]
        if "contoso" in code.lower() and "contoso-sources" not in code:
            raise AssertionError(f"the Makefile names a product: {line.strip()!r}")

    # The Makefile was the only thing checked, and that is how a whole dbt
    # PROJECT survived the split sitting in gold/: `dbt_project.yml` and
    # `profiles.yml`, byte-identical to the product's copies, naming
    # `contoso_gold`. A platform holds no product artifact, and a dbt project
    # is one -- it declares models, materializations and a profile, which are
    # the product's decisions, not the platform's.
    #
    # ./product is exempt: that is the mount point where a product is supplied,
    # so a dbt project appearing THERE is the product being run, not the
    # platform holding one.
    #
    # ./data is exempt for the same reason one step further out: it is the
    # EMULATOR'S OWN STATE DIRECTORY, bind-mounted into the container and
    # gitignored, alongside the PAT, the OIDC keys and the secret store. Now
    # that gold runs as a Jobs `dbt_task`, the step uploads the product's
    # project to `/Workspace/contoso/gold`, and the workspace store is a
    # directory on this side of the mount -- so a real dbt project appears
    # under `data/workspace/` on every run. That is the product being RUN,
    # recorded by the emulator, not the platform holding a product: nothing
    # there is authored here and nothing there is committed.
    #
    # Exempted by name rather than by widening the glob, because the thing this
    # test exists to catch -- a project checked in under `gold/`, which is
    # exactly how one survived the split -- must still fail.
    strays = [
        d.relative_to(ROOT).as_posix()
        for d in ROOT.rglob("dbt_project.yml")
        if d.relative_to(ROOT).parts[0] not in ("product", "data")
        and ".venv" not in d.parts
    ]
    assert not strays, (
        f"the platform holds a dbt project: {strays}. Models, macros and the "
        f"profile belong to whichever product PRODUCT points at."
    )

    # And it must not install dbt either: a dependency the platform cannot use
    # is a dependency whose advisories it still inherits, which is exactly how
    # four sqlparse CVEs arrived here through dbt-core.
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for line in pyproject.splitlines():
        code = line.split("#", 1)[0]
        if "dbt" in code.lower():
            raise AssertionError(f"the platform declares a dbt dependency: {line.strip()!r}")


def test_the_product_is_supplied_as_a_path():
    """PRODUCT is how the platform learns what to run, and it is a PATH.

    A name would mean this platform could only ever run one product, which is
    the property the family is trying to demonstrate is false.
    """
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"^PRODUCT \?= \./product$", makefile, re.M), (
        "PRODUCT must default to the ./product mount point"
    )
    # `cd &&` is not available on cmd.exe, which is why the steps run through
    # `uv run --directory` instead.
    assert "--directory $(PRODUCT)" in makefile


def test_no_image_comes_from_a_registry_the_family_does_not_trust():
    """G44: OpenMetadata shipped from docker.getcollate.io and took this
    nightly down twice in one morning.

    That registry is backed by neither Docker Hub nor GHCR, and a pull failure
    there reads as a broken governance step rather than as somebody else's
    outage. The images are mirrored into ghcr.io/calvinchengx by
    `calvinchengx/emulators` (`mirrors.json`, `scripts/mirror_images.py`), which
    copies the manifest index and records the digest the registry serves.

    AN ALLOWLIST, NOT A BAN ON ONE NAME. Asserting `getcollate` is absent would
    pass the day somebody adds a different vendor registry, which is the same
    defect one name later. This asks the opposite question: every image must
    come from somewhere the family already depends on being up.

    A VALUE THAT IS ENTIRELY A VARIABLE IS RESOLVED, not skipped. `${X}` hides
    the host completely, so a check that ignored those would be a check with a
    hole exactly where an unreviewed image would sit.
    """
    trusted = {
        # The family's own, and the mirrors it keeps there.
        "ghcr.io",
        # Docker Hub, which is what a bare `name/image` resolves to.
        "docker.io",
        "mcr.microsoft.com",
    }

    env = {}
    for line in (ROOT / "versions.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()

    def host_of(ref: str) -> str | None:
        head = ref.split("/")[0]
        return head if ("." in head or ":" in head) else "docker.io"

    bad = []
    for path in sorted((ROOT / "compose").rglob("*.yml")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not stripped.startswith("image:"):
                continue
            ref = stripped.split(":", 1)[1].strip()
            whole = re.fullmatch(r"\$\{(\w+)(?::[?-][^}]*)?\}", ref)
            if whole:
                name = whole.group(1)
                if name not in env:
                    bad.append(f"{path.name}:{n}: ${{{name}}} is not in versions.env, "
                               f"so nothing here can tell which registry it names")
                    continue
                ref = env[name]
            host = host_of(ref)
            if host not in trusted:
                bad.append(f"{path.name}:{n}: {host} is not a registry the family "
                           f"trusts to be up ({ref})")
    assert not bad, "untrusted registries:\n  " + "\n  ".join(bad)


def test_openmetadata_comes_from_the_mirror():
    """The allowlist above would also pass if OpenMetadata simply vanished.

    So this names the thing G44 is about: the catalog's two images, from the
    family's registry, by the tag versions.env pins.
    """
    gov = (ROOT / "compose" / "governance.yml").read_text(encoding="utf-8")
    images = [ln.strip() for ln in gov.splitlines() if ln.strip().startswith("image:")]
    for name in ("openmetadata-server", "openmetadata-postgresql"):
        assert any(f"ghcr.io/calvinchengx/{name}:" in i for i in images), (
            f"the governance stack does not pull {name} from the family's registry"
        )
    assert not any("getcollate" in i for i in images), (
        "an image still comes straight from the vendor registry"
    )


def test_the_committed_vendor_ports_match_what_the_generator_emits():
    """`vendor-ports.json` is the only committed record of these host ports.

    The vendor compose fragment is GENERATED at `make up` and gitignored, so
    nothing in any repository recorded which host ports it publishes: the
    family registry could not see them, and the check that refuses two members
    claiming one host port was blind to them. This file is what the hub reads;
    this test is what keeps it true.

    IT REFUSES RATHER THAN SKIPS when the declaration is missing. A skip here
    would be invisible in CI, and CI is the only place it matters -- the first
    version skipped, and this repository's test job did not check out
    contoso-sources at all, so the check would have passed by never running.
    The job now places it beside this one, the way the Fabric platform's
    already did.

    Regenerate with:
        uv run --no-project python scripts/sources.py \\
            ../contoso-sources/sources.yaml $(cd ../contoso-sources && pwd) \\
            --ports > vendor-ports.json
    """
    import json
    import subprocess
    import sys

    root = pathlib.Path(__file__).resolve().parents[1]
    sources = pathlib.Path(os.environ.get("SOURCES", root.parent / "contoso-sources"))
    assert (sources / "sources.yaml").is_file(), (
        f"no contoso-sources declaration at {sources}; this test generates the "
        "vendor fragment from it. Clone it beside this repository or set SOURCES."
    )

    out = subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "sources.py"),
            str(sources / "sources.yaml"),
            str(sources),
            "--ports",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    emitted = json.loads(out.stdout)
    assert emitted, (
        "the generator published no host ports — this check would be vacuous"
    )
    committed = json.loads((root / "vendor-ports.json").read_text(encoding="utf-8"))
    assert committed == emitted, (
        "vendor-ports.json is stale; regenerate it (see this test's docstring)"
    )
# --- digest pins ---------------------------------------------------------------
#
# `emulator-sail:X` and `emulator-spark-agent:X` are tagged for the dependency
# they carry, not their content, so one tag can be republished over different
# code. Docker also IGNORES the tag in `repo:tag@sha256:...` — the digest wins
# silently — which makes a digest left behind worse than no digest at all.

def _scripts():
    sys.path.insert(0, str(ROOT / "scripts"))


def test_every_pinned_image_has_both_a_version_and_a_digest():
    _scripts()
    from digests import PINS

    text = (ROOT / "versions.env").read_text(encoding="utf-8")
    for prefix in PINS:
        assert re.search(rf"^{prefix}_VERSION=.+$", text, re.M), prefix
        assert re.search(rf"^{prefix}_DIGEST=sha256:[0-9a-f]{{64}}$", text, re.M), prefix


def test_the_compose_file_fetches_those_images_by_digest():
    _scripts()
    from digests import PINS

    compose = (ROOT / "compose" / "docker-compose.yml").read_text(encoding="utf-8")
    for prefix, image in PINS.items():
        for line in compose.splitlines():
            if f"image: {image}:" in line:
                assert f"@${{{prefix}_DIGEST" in line, f"pulled by tag alone: {line.strip()}"
                break
        else:
            raise AssertionError(f"{image} is not referenced in the compose file")


def test_a_release_moves_the_emulator_digest_with_its_version(tmp_path):
    """End to end through main(), because that is where the wiring can be cut.

    Asserting the helper alone would pass with the call deleted from `main()` —
    measured on the sibling repo, where exactly that mutation left every other
    test green.
    """
    _scripts()
    import set_release

    versions = tmp_path / "versions.env"
    versions.write_text((ROOT / "versions.env").read_text(encoding="utf-8"),
                        encoding="utf-8")
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text((ROOT / "pyproject.toml").read_text(encoding="utf-8"),
                         encoding="utf-8")
    fake = "sha256:" + "d" * 64
    saved = (set_release.VERSIONS, set_release.PYPROJECT,
             set_release.digests.digest_of, sys.argv)
    try:
        set_release.VERSIONS = versions
        set_release.PYPROJECT = pyproject
        set_release.digests.digest_of = lambda image, tag: fake
        sys.argv = ["set_release.py", "9.9.9"]
        set_release.main()
    finally:
        (set_release.VERSIONS, set_release.PYPROJECT,
         set_release.digests.digest_of, sys.argv) = saved

    written = versions.read_text(encoding="utf-8")
    assert re.search(r"^DATABRICKS_EMULATOR_VERSION=9\.9\.9$", written, re.M)
    assert re.search(rf"^DATABRICKS_EMULATOR_DIGEST={fake}$", written, re.M), (
        "the version moved and the digest did not — docker would pull the old image")


def test_refresh_digests_rewrites_every_pin(tmp_path):
    """The hand-bump path. SAIL and SPARK_AGENT follow fabric-emulator's
    cadence, so a person edits those versions and nothing else would move the
    digests."""
    _scripts()
    import digests as d
    import refresh_digests

    versions = tmp_path / "versions.env"
    versions.write_text((ROOT / "versions.env").read_text(encoding="utf-8"),
                        encoding="utf-8")
    fake = "sha256:" + "e" * 64
    saved = (refresh_digests.VERSIONS, d.digest_of, refresh_digests.digest_of)
    try:
        refresh_digests.VERSIONS = versions
        refresh_digests.digest_of = lambda image, tag: fake
        refresh_digests.main()
    finally:
        refresh_digests.VERSIONS, d.digest_of, refresh_digests.digest_of = saved

    written = versions.read_text(encoding="utf-8")
    for prefix in d.PINS:
        assert re.search(rf"^{prefix}_DIGEST={fake}$", written, re.M), prefix
