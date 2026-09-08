#!/usr/bin/env python3
"""Build every first-party image, and prove the running stack answers from outside it.

    python scripts/ci_image_proof.py list      the image inventory, and who builds each
    python scripts/ci_image_proof.py build     build EVERY first-party image
    python scripts/ci_image_proof.py probe     HTTP-drive the stack that is up

WHY THIS EXISTS. Neither CI pipeline used to build a single first-party image or start a
single first-party container. `make verify` is an in-process gate: it imports from a
checkout where every package resolves, and it never opens a Dockerfile. A commit that
breaks an image — the most repeated defect class in this repository — passed both
pipelines green, and the break was found by a human running `docker compose up`.

`proxyshop_support/tests/test_artifact_copyset.py` closes as much of that as static
analysis can: it reads each Dockerfile's COPY set and proves the modules an image ships can
import what they import. It cannot prove the image BUILDS (a `pip install` that no longer
resolves, a base tag that vanished, a build stage whose npm registry call fails) and it
cannot prove the container STARTS. Those need a daemon, and that is this file's job.

DISCOVERY IS NOT RE-DERIVED HERE. The set of first-party images is imported from the gate
module, deliberately, because a second and slightly different walk of the tree is the exact
defect this all began with: `images()` globbed the filename `Dockerfile`, the tree held ten
files, and `apps/buyer/Dockerfile.web` was graded by nothing. One discovery function, used
by both the static gate and the pipeline, cannot drift from itself.

WHAT `build` COVERS THAT `docker compose build` DOES NOT. Compose builds the images its
services declare. An image in the tree that no fragment references yet — `apps/merchant/
Dockerfile.web` is one today — is built by nothing, which is how an image lands in the repo
broken and stays that way. This builds the compose set through compose (one BuildKit
session, so the eight-image byte-identical pip prologue is downloaded once) and then builds
every orphan directly, and REFUSES if anything discovered went unbuilt.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from proxyshop_support.tests.test_artifact_copyset import (  # noqa: E402
    KIND_STATIC_WEB,
    compose_declared_dockerfiles,
    dockerfile_labels,
    dockerfile_text,
    image_kind,
    parse_stages,
)

#: Read out of the compose fragments rather than listed, for the same reason as everything
#: else here. Kept in one place because three subcommands want it.
COMPOSE_FRAGMENT_ROOT = ROOT / "docker-compose.yml"

#: How long a single HTTP probe may take. The stack is local; anything slower than this is
#: a service that is not answering, not a slow network.
PROBE_TIMEOUT = 15.0


def _fail(message: str) -> int:
    print("", file=sys.stderr)
    print("=" * 78, file=sys.stderr)
    print(message.rstrip(), file=sys.stderr)
    print("=" * 78, file=sys.stderr)
    return 1


def _run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    print(f"+ {' '.join(argv)}", flush=True)
    return subprocess.run(argv, cwd=ROOT, text=True, **kwargs)


def _compose_services() -> dict[str, dict[str, Any]]:
    """``{service: definition}`` across the root file and every fragment it includes."""
    import yaml

    bodies: dict[str, dict[str, Any]] = {}
    root = yaml.safe_load(COMPOSE_FRAGMENT_ROOT.read_text(encoding="utf-8")) or {}
    sources = [COMPOSE_FRAGMENT_ROOT] + [ROOT / str(entry) for entry in root.get("include") or []]
    for path in sources:
        if not path.is_file():
            continue
        parsed = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for name, definition in (parsed.get("services") or {}).items():
            bodies[str(name)] = definition or {}
    return bodies


def _declares_ports(definition: dict[str, Any]) -> bool:
    """Does this service publish anything at all? A YES/NO question, not a port number.

    The numbers are deliberately NOT parsed out of the YAML. A first version did, and it
    was wrong in two directions at once. `docker-compose.yml` states as a design goal that
    every port is "env-overridable so a second checkout can move the whole block", and
    reading the `${VAR:-8080}` default ignores the override — so a runner that used the
    documented mechanism would have this probe hammering the vacated ports and declaring
    the whole stack dead. And `127.0.0.1:8080:8080`, `[::1]:8080:8080`, a port RANGE, the
    long `{target:, published:}` form and a bare `"8080"` (which is a RANDOM host port, not
    port 8080) each parsed to nothing or to a lie, silently.

    The running daemon knows the answer. :func:`_running_ports` asks it.
    """
    return bool(definition.get("ports"))


def _bind_mounts(definition: dict[str, Any]) -> list[str]:
    """The service's HOST-path volume sources — the ones a remote daemon cannot fill."""
    found: list[str] = []
    for entry in definition.get("volumes") or []:
        if isinstance(entry, dict):
            if str(entry.get("type", "volume")) == "bind":
                found.append(str(entry.get("source", "")))
            continue
        source = str(entry).split(":", 1)[0]
        if source.startswith((".", "/", "~")):
            found.append(source)
    return found


def _is_job(definition: dict[str, Any]) -> bool:
    """A run-to-completion container: it exits, so `up --wait` must never be given it."""
    return str(definition.get("restart", "")).strip('"') == "no"


def long_running_first_party(exclude_bind_mounts: bool = False) -> list[str]:
    """The services a pipeline should BOOT, derived from compose rather than restated.

    Every CI file that lists service names by hand is a copy of compose that nothing
    checks: add a service to a fragment and it is not started, not probed, and no gate goes
    red. Both pipelines call this instead, so the boot list and the thing the probe expects
    to answer are the same list, computed once, from the fragments themselves.

    Three filters, each of them a measured requirement rather than a preference:

    * FIRST-PARTY only — the datastores are third-party images that do not speak HTTP, and
      `scripts/ci_datastore_proof.py` proves those in their own protocols.
    * NOT A JOB — `sim`, `seller-reference` and `corpus-loader` have `restart: "no"` and
      exit when done. `up --wait` sees them exit and fails the whole command over a stack
      in which every server is healthy. They are run separately, where the exit status is
      the thing being measured.
    * PUBLISHES A PORT — something with no published port cannot be probed from outside,
      so booting it would add a container this file can make no claim about.

    ``exclude_bind_mounts`` drops the services configured by a mounted document. Under
    docker-in-docker a bind source resolves on the DAEMON's filesystem, and Docker answers
    a missing one by silently creating an empty directory — so those services come up
    reading blank configuration and report healthy over it. Derived, so the GitLab boot
    list cannot drift from the reason it is shorter.
    """
    services = _compose_services()
    first_party = {
        service
        for label in dockerfile_labels()
        for service in compose_declared_dockerfiles().get(label, ())
    }
    chosen = []
    for name in sorted(first_party):
        definition = services.get(name, {})
        if _is_job(definition) or not _declares_ports(definition):
            continue
        if exclude_bind_mounts and _bind_mounts(definition):
            continue
        chosen.append(name)
    return chosen


def _compose_ps() -> dict[str, list[int]]:
    """``{running service: its published HOST ports}``, asked of the daemon.

    ``docker compose ps --format json`` emits NDJSON on some versions and a single JSON
    array on others; both are read. ``Publishers`` carries the ports the daemon ACTUALLY
    published, which is the only source that survives an env override or any of the port
    spellings compose accepts.
    """
    done = _run(
        ["docker", "compose", "ps", "--format", "json"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if done.returncode != 0:
        raise RuntimeError(f"`docker compose ps` failed:\n{done.stderr}")
    records: list[dict[str, Any]] = []
    for line in (done.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        records.extend(parsed if isinstance(parsed, list) else [parsed])
    running: dict[str, list[int]] = {}
    for record in records:
        ports = {
            int(publisher["PublishedPort"])
            for publisher in record.get("Publishers") or []
            if publisher.get("PublishedPort")
        }
        running.setdefault(str(record.get("Service")), []).extend(sorted(ports))
    return {name: sorted(set(ports)) for name, ports in running.items()}


# =====================================================================================
# list / build
# =====================================================================================


def _inventory() -> list[tuple[str, str, tuple[str, ...]]]:
    built_by = compose_declared_dockerfiles()
    return [(label, image_kind(label), built_by.get(label, ())) for label in dockerfile_labels()]


def cmd_list(_args: argparse.Namespace) -> int:
    rows = _inventory()
    width = max(len(label) for label, _kind, _services in rows)
    for label, kind, services in rows:
        who = ", ".join(services) if services else "NO COMPOSE SERVICE (built directly)"
        print(f"{label:<{width}}  {kind:<11}  {who}")
    print(f"\n{len(rows)} first-party images.")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    rows = _inventory()
    if not rows:
        return _fail("discovery found no Dockerfiles at all; the pipeline is grading nothing.")

    services = sorted({service for _label, _kind, names in rows for service in names})
    unbuilt: list[str] = []

    if services:
        # ONE compose invocation, not one per service: the eight python images share a
        # byte-identical pip prologue on purpose, and a single BuildKit session downloads
        # that layer once instead of eight times.
        done = _run(["docker", "compose", *args.profile_flags, "build", *services])
        if done.returncode != 0:
            return _fail(
                "`docker compose build` failed. One of this repo's own images no longer "
                "builds — the single most repeated defect class here, and the one no "
                "in-process gate can see. The failing step is above."
            )

    for label, _kind, names in rows:
        if names:
            continue
        # An image no fragment references. It is still first-party, it is still in the
        # tree, and until it is wired into compose nothing else would ever build it.
        tag = "proxyshop-ci/" + label.replace("/", "-").replace(".", "-").lower()
        done = _run(["docker", "build", "-f", label, "-t", tag, "."])
        if done.returncode != 0:
            unbuilt.append(f"{label} (no compose service references it; built directly)")

    if unbuilt:
        return _fail("these first-party images do not build:\n  " + "\n  ".join(unbuilt))
    print(f"\nOK: all {len(rows)} first-party images build.")
    return 0


# =====================================================================================
# probe — the claim `docker compose up --wait` cannot make
# =====================================================================================


def _http(url: str) -> tuple[int, bytes, str | None]:
    """``(status, body, error)``. A 404 is an ANSWER; only a refused connection is not."""
    request = urllib.request.Request(url, headers={"User-Agent": "proxyshop-ci"})
    try:
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT) as response:  # noqa: S310
            return int(response.status), response.read(), None
    except urllib.error.HTTPError as error:  # an HTTP status IS a server answering
        return int(error.code), error.read(), None
    except Exception as error:  # noqa: BLE001 - a refused socket, a timeout, a reset
        return 0, b"", f"{type(error).__name__}: {error}"


def _proxied_prefixes(conf: Path) -> list[tuple[str, str | None]]:
    """``(location prefix, the compose service it proxies to)`` for each proxying block.

    Derived rather than listed: the buyer's SPA is namespaced under ``/buyer/`` today, and
    a probe that hard-coded that string would keep passing after the prefix moved.

    The UPSTREAM is read too, and that is what lets this file tell "the API is broken" from
    "the API was deliberately not started". `deploy/buyer-web/nginx.conf` writes the target
    through a variable (``set $buyer_api http://buyer-svc:8081; proxy_pass
    $buyer_api$request_uri;``) so that nginx re-resolves the name instead of caching one
    container's IP forever; the host inside that URL is a compose SERVICE NAME, and whether
    that service is running is a question :func:`_compose_ps` can answer.
    """
    text = conf.read_text(encoding="utf-8")
    found: list[tuple[str, str | None]] = []
    for block in re.finditer(r"location\s+(/\S*)\s*\{(.*?)\n\s*\}", text, re.DOTALL):
        body = block.group(2)
        if "proxy_pass" not in body:
            continue
        upstream = re.search(r"https?://([A-Za-z0-9_.-]+)", body)
        found.append((block.group(1), upstream.group(1) if upstream else None))
    return found


def _static_web_probes(host: str, running: dict[str, list[int]]) -> list[str]:
    """The checks a static-web image needs and no healthcheck makes.

    The compose healthcheck for ``buyer-web`` fetches ``/`` and is satisfied the moment
    nginx answers. Four things it therefore cannot see, each of which has a green build, a
    healthy container and a dead product behind it:

    * ``/`` answers but serves the stock nginx page, because the bundle landed elsewhere;
    * a DEEP LINK 404s, because ``try_files`` is missing — this is the failure the whole
      separate ``buyer-web`` service exists to avoid, and a magic-link sign-in dies on it;
    * the reverse-proxied prefix is answered by the SPA's own fallback instead of by the
      API, so every ``fetch`` in the page silently receives ``index.html``. The page loads,
      looks perfect, and nothing works;
    * the prefix reaches nginx and nginx cannot reach the API — a 502. An adversarial
      re-measurement caught this file passing that: the first version reported a problem
      only when the body EQUALLED index.html, and a 502 page is not index.html, so a
      buyer-web with no reachable API was graded clean. The status is now required to be a
      status the API could plausibly have produced.

    THE CHECK ALSO REFUSES TO GO QUIET. Its subject — the ``location`` blocks that carry a
    ``proxy_pass`` — is read out of the very config file it is grading, so DELETING the
    proxy block would leave nothing to iterate and print "the SPA origin checks passed".
    An image that serves a bundle and proxies nothing is reported, not skipped.
    """
    problems: list[str] = []
    built_by = compose_declared_dockerfiles()
    for label in dockerfile_labels():
        if image_kind(label) != KIND_STATIC_WEB:
            continue
        final = parse_stages(dockerfile_text(label), label)[-1]
        confs = [
            ROOT / source
            for _position, source, destination in final.copies
            if destination.startswith("/etc/nginx") and (ROOT / source).is_file()
        ]
        for service in built_by.get(label, ()):
            ports = running.get(service)
            if not ports:
                continue  # not running in this job; the caller decides whether that is ok
            if not confs:
                problems.append(
                    f"{service} builds from {label}, which copies no readable nginx config "
                    f"out of the build context, so this probe cannot know what it should be "
                    f"serving and is not going to pretend it checked."
                )
                continue
            for port in ports:
                base = f"http://{host}:{port}"
                status, index, error = _http(f"{base}/")
                if error or status != 200 or not index.strip():
                    problems.append(
                        f"{service} GET {base}/ -> {error or status}, {len(index)} bytes. The "
                        f"container is healthy and serving no page."
                    )
                    continue
                deep = f"{base}/ci-probe/deep-link-that-is-not-a-file"
                status, _body, error = _http(deep)
                if error or status != 200:
                    problems.append(
                        f"{service} GET {deep} -> {error or status}. A path that is not a "
                        f"file must fall back to index.html; without that the SPA's own "
                        f"routes 404 and the mailed sign-in link is dead on arrival."
                    )
                prefixes = [p for conf in confs for p in _proxied_prefixes(conf)]
                if not prefixes:
                    problems.append(
                        f"{service}'s config reverse-proxies NOTHING. Every endpoint the SPA "
                        f"calls is a bare relative path on a plain fetch and the API installs "
                        f"no CORS, so a page served without a proxy block is a page whose "
                        f"every request lands on the static handler. (This branch exists "
                        f"because the prefixes are read out of the same file: deleting the "
                        f"proxy block would otherwise leave this check with nothing to do "
                        f"and a clean report.)"
                    )
                for prefix, upstream in prefixes:
                    url = f"{base}{prefix.rstrip('/')}/ci-probe-not-a-real-route"
                    status, body, error = _http(url)
                    if error:
                        problems.append(f"{service} GET {url} -> {error}")
                    elif body == index:
                        problems.append(
                            f"{service} GET {url} returned the SPA's index.html, so "
                            f"{prefix} is NOT reaching the API — nginx answered it with "
                            f"the try_files fallback. Every fetch in the page receives "
                            f"HTML where it expects JSON, and the page still renders."
                        )
                    elif status >= 500 and upstream in running:
                        # A 5xx is only a finding when the upstream IS up: then nginx is
                        # proxying and the API is failing, so the page loads and every call
                        # it makes dies. Where the upstream was deliberately not started —
                        # the docker-in-docker job cannot configure `buyer-svc` — a 502 is
                        # the expected answer and the surviving claim is the one above: the
                        # prefix is a PROXY block and not the SPA fallback.
                        problems.append(
                            f"{service} GET {url} -> {status} while its upstream "
                            f"{upstream!r} is running. nginx is proxying {prefix} and the "
                            f"API behind it is not answering, so the page loads and every "
                            f"call it makes fails. A body that merely differs from "
                            f"index.html is NOT evidence the API is reachable: an nginx 502 "
                            f"page differs too, and used to pass here."
                        )
                    elif status >= 500:
                        print(
                            f"    {service:<32} {prefix} -> {status} (upstream {upstream!r} "
                            f"is not running in this job; the proxy block is present, the "
                            f"API path is graded where that service runs)"
                        )
    return problems


def cmd_probe(args: argparse.Namespace) -> int:
    expected = long_running_first_party(exclude_bind_mounts=args.exclude_bind_mounts)
    if not expected:
        return _fail(
            "no first-party service was derived from the compose fragments, so this probe "
            "would report a clean stack by measuring nothing. `cmd_build` refuses the same "
            "way; a probe that can pass vacuously is not a probe."
        )
    try:
        running = _compose_ps()
    except RuntimeError as refused:
        return _fail(str(refused))

    problems: list[str] = []
    missing = [service for service in expected if service not in running]
    if missing:
        problems.append(
            f"these first-party services should be up and are not in `docker compose ps`: "
            f"{missing}. The list is DERIVED from the compose fragments, so this is either a "
            f"container that failed to start or a service somebody added to a fragment and "
            f"to no boot list."
        )

    answered = 0
    for service in expected:
        ports = running.get(service) or []
        if service in running and not ports:
            problems.append(
                f"{service} is running and publishes no port the daemon will admit to, so "
                f"nothing outside its network can reach it."
            )
        for port in ports:
            url = f"http://{args.host}:{port}/"
            status, _body, error = _http(url)
            if error:
                problems.append(
                    f"{service} publishes {port} and {url} did not answer: {error}. The "
                    f"container may be healthy INSIDE its network and unreachable from "
                    f"outside it, which is a deployment nobody can use."
                )
            else:
                answered += 1
                print(f"    {service:<32} {url} -> {status}")

    problems.extend(_static_web_probes(args.host, running))

    if problems:
        return _fail(
            "the stack is up and these services are not answering:\n  " + "\n  ".join(problems)
        )
    print(
        f"\nOK: all {len(expected)} derived first-party services are up, {answered} published "
        f"port(s) answered, and the SPA origin checks passed."
    )
    return 0


def cmd_services(args: argparse.Namespace) -> int:
    """Print the boot list, so a pipeline never restates one."""
    print(" ".join(long_running_first_party(exclude_bind_mounts=args.exclude_bind_mounts)))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="the image inventory and who builds each").set_defaults(
        func=cmd_list
    )

    build = sub.add_parser("build", help="build every first-party image")
    build.add_argument(
        "--profile",
        dest="profiles",
        action="append",
        default=[],
        help="a compose profile to include (repeatable). Without these, profiled services "
        "-- and therefore their images -- are not built.",
    )
    build.set_defaults(func=cmd_build)

    probe = sub.add_parser("probe", help="HTTP-drive the running stack from outside it")
    probe.add_argument(
        "--host",
        default="localhost",
        help="where compose published its ports. `localhost` on a VM runner; the docker-in-"
        "docker service alias on a GitLab runner, where published ports land on the daemon's "
        "container and not on the job's.",
    )
    probe.add_argument(
        "--exclude-bind-mounts",
        action="store_true",
        help="expect only the services a remote daemon can configure. Use it wherever the "
        "boot list was produced with the same flag, so the two cannot disagree.",
    )
    probe.set_defaults(func=cmd_probe)

    services = sub.add_parser(
        "services", help="the boot list, derived from compose so no pipeline restates it"
    )
    services.add_argument(
        "--exclude-bind-mounts",
        action="store_true",
        help="drop the services configured by a mounted document — under docker-in-docker a "
        "bind source resolves on the daemon's filesystem, and Docker fills a missing one with "
        "an empty directory rather than an error.",
    )
    services.set_defaults(func=cmd_services)

    args = parser.parse_args()
    args.profile_flags = [
        flag for profile in getattr(args, "profiles", []) for flag in ("--profile", profile)
    ]
    result = args.func(args)
    return int(result)


if __name__ == "__main__":
    raise SystemExit(main())
