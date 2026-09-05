#!/usr/bin/env python3
"""Assemble compose files. Logic lives here so the Makefile survives cmd.exe."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "compose" / ".generated"
FILES = ["compose/docker-compose.yml"]
if os.environ.get("GOVERNANCE", "1") == "1":
    FILES.append("compose/governance.yml")


def sources_dir() -> Path:
    """The contoso-sources checkout this stack pulls its vendors from.

    A SIBLING PATH, and the one place in this repository where that is right.
    Everything else installs from a published wheel because this platform must
    build on its own -- but the vendors are not a dependency of this platform,
    they are the world outside it, and they are mounted into containers as
    bytes rather than imported as code. Overridable, because pointing this at
    real vendors is exactly what production does.
    """
    return Path(os.environ.get("SOURCES", ROOT.parent / "contoso-sources")).resolve()


def vendor_fragment() -> Path:
    """Generate the vendor compose fragment from the sources declaration.

    Generated rather than checked in, so this repository cannot hold a stale
    copy of another repository's vendor list. If contoso-sources adds a vendor,
    the next `make up` stands it up.
    """
    src = sources_dir()
    decl = src / "sources.yaml"
    if not decl.exists():
        sys.exit(
            f"no vendor declaration at {decl}.\n\n"
            f"This platform pulls from the vendors contoso-sources declares --\n"
            f"the same ones fabric-platform-notebook-pipelines pulls from, which is what\n"
            f"makes the two runtimes' gold numbers comparable. Clone it beside\n"
            f"this repository, or set SOURCES=/path/to/contoso-sources."
        )
    # The bytes, not just the declaration. Without `make sources` over there the
    # vendors still START -- mokapi falls back to generating bodies from the
    # OpenAPI schema -- and every ingest step would land invented data that
    # looks entirely plausible until the numbers are compared. Refusing here is
    # the difference between a clear message and a silent wrong answer.
    data = src / "_data"
    if not data.is_dir() or not any(data.iterdir()):
        sys.exit(
            f"{data} is empty -- the vendors have no bytes to serve.\n\n"
            f"Run `make sources` in {src} first. Without it mokapi does not\n"
            f"fail: it generates bodies from the OpenAPI schema and answers\n"
            f"every request 200, so this pipeline would land invented data."
        )
    BUILD.mkdir(parents=True, exist_ok=True)
    out = BUILD / "sources.json"
    frag = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "sources.py"), str(decl), str(src)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    out.write_text(frag, encoding="utf-8")
    return out


def main() -> int:
    args = sys.argv[1:]
    files = list(FILES)
    files.append(str(vendor_fragment().relative_to(ROOT)))
    cmd = [
        "docker",
        "compose",
        "--env-file",
        "versions.env",
        "--profile",
        "governance",
    ]
    for f in files:
        cmd.extend(["-f", f])
    cmd.extend(args)
    env = os.environ.copy()
    env.setdefault("DELTA_DATA", str(Path("/tmp/contoso-dbx-delta")))
    Path(env["DELTA_DATA"]).mkdir(parents=True, exist_ok=True)
    env.setdefault("DATABRICKS_DATA", str(ROOT / "data"))
    Path(env["DATABRICKS_DATA"]).mkdir(parents=True, exist_ok=True)
    os.chmod(env["DATABRICKS_DATA"], 0o777)
    rc = subprocess.call(cmd, cwd=ROOT, env=env)
    if args and args[0] == "up":
        rc = wait_for_jobs(cmd[: -len(args)], env, rc)
        if rc != 0:
            dump_failure(cmd[: -len(args)], env)
    return rc


def wait_for_jobs(base: list[str], env: dict, rc: int) -> int:
    """`up --wait` starts the one-shot jobs. It does not wait for them to DO
    anything, and the next step needs them finished.

    PORTED FROM snowflake-platform-tasks, which paid for this in both
    directions before getting it right. This cell has the same two steps that
    are not servers -- `contoso-erp-seed` replays the vendor's history into its
    database, and `om-migrate` migrates OpenMetadata's schema -- and its
    nightly acceptance failed the same way on 2026-08-21:

        Container compose-om-migrate-1  Exited
        container compose-contoso-erp-seed-1 exited (0)
        make: *** [Makefile:41: up] Error 1

    A job that has FINISHED is "not running" to `--wait`, so `up` returned 1
    on a stack that had come up correctly, and CI stopped before `make verify`
    with the failure reported against a step that never ran. The run the day
    before passed: whether the seed had finished by the time `--wait` looked
    is a race, which is why this was green once and red the next morning.

    The other direction is worse and is why "accept running" is not the fix:
    a job STILL RUNNING is "started", so `up` would return while the ERP seed
    was mid-replay and ingest would read a database with nothing in it.

    So the wait is on completion: every `restart: no` service must have exited,
    and exited 0. Bounded, because a seed that never finishes is a fault to
    report rather than to hang on.
    """
    jobs = one_shot_services(base, env)
    if not jobs:
        return rc
    deadline = time.time() + 600.0
    while True:
        states = service_states(base, env)
        if states is None:
            return rc
        pending = [j for j in jobs if states.get(j, ("", 0))[0] != "exited"]
        failed = [
            f"{j}: exited {states[j][1]}"
            for j in jobs
            if states.get(j, ("", 0))[0] == "exited" and states[j][1] != 0
        ]
        if failed:
            print("compose: " + "; ".join(failed))
            return rc or 1
        if not pending:
            break
        if time.time() >= deadline:
            print(f"compose: still running after 600s: {', '.join(pending)}")
            return rc or 1
        time.sleep(2.0)

    # A service that has exited 0 is fine whether or not compose declares it
    # `restart: no`: om-migrate does not, and flagging it broken for finishing
    # its job was the snowflake port's first mistake in the other direction.
    broken = [
        f"{n}: {s} ({c})"
        for n, (s, c) in service_states(base, env).items()
        if s not in ("running", "restarting") and not (s == "exited" and c == 0)
    ]
    if broken:
        print("compose: " + "; ".join(broken))
        return rc
    print(f"compose: up -- services running, jobs finished ({', '.join(sorted(jobs))})")
    return 0


def one_shot_services(base: list[str], env: dict) -> set[str]:
    """Services compose declares `restart: no` -- steps, not servers."""
    out = subprocess.run(
        base + ["config", "--format", "json"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return set()
    try:
        cfg = json.loads(out.stdout)
    except json.JSONDecodeError:
        return set()
    return {n for n, s in cfg.get("services", {}).items() if s.get("restart") == "no"}


def service_states(base: list[str], env: dict):
    """{service: (state, exit_code)} for everything compose knows about."""
    ps = subprocess.run(
        base + ["ps", "-a", "--format", "json"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if ps.returncode != 0 or not ps.stdout.strip():
        return None
    states = {}
    for line in ps.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            svc = json.loads(line)
        except json.JSONDecodeError:
            return None
        states[svc.get("Service", "?")] = (svc.get("State", ""), svc.get("ExitCode", 0))
    return states


def dump_failure(base: list[str], env: dict) -> None:
    """What the containers said on the way down.

    `compose up` resolves depends_on itself and reports only `dependency
    failed to start` -- WHICH container, and nothing about why it exited. That
    is how G48, a released emulator that did not boot in a sibling stack,
    survived a release and three CI runs without a single line of diagnosis.
    The logs exist at this moment and are gone as soon as anyone runs
    `make down`, which CI does in its cleanup step.

    `ps -a` first because it names which container died and with what code; the
    logs then say what it said on the way out. Both are bounded (`--tail`) so a
    noisy stack cannot bury the failure they exist to explain.

    check=False throughout, and no return value: this runs on a path that is
    already failing, and a diagnostic that can raise would replace the failure
    it was called to explain.
    """
    print("platform: the stack did not come up. what the containers said:", flush=True)
    subprocess.run([*base, "ps", "-a"], cwd=ROOT, env=env, check=False)
    subprocess.run(
        [*base, "logs", "--no-color", "--tail=80"], cwd=ROOT, env=env, check=False
    )


if __name__ == "__main__":
    raise SystemExit(main())
