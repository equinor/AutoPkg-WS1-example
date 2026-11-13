#!/usr/bin/env python3
"""Generate and register local WS1 importer, assigner and pruner overrides.

Given an upstream WS1 importer recipe from the WorkSpaceOneImporter-recipes
repository, this tool:

1. Discovers the upstream importer recipe and, where present, its matching
   ``.ws1-assigner`` and ``.ws1-pruner`` sibling recipes.
2. Runs ``autopkg make-override --format=yaml`` for each into a temporary
   directory so AutoPkg computes correct ``ParentRecipeTrustInfo``.
3. Applies project defaults (``templates/overrides_defaults.local.yaml``, falling back
   to the generic ``overrides_defaults.yaml`` with a notice): upstream
   ``*_HERE`` placeholders become ``none`` (values are supplied at runtime via
   ``AUTOPKG_WS1_*`` env vars), staged ``ws1_app_assignments`` for the
   assigner, and retention settings for the pruner.
4. Validates the result and writes it to ``autopkg/overrides/`` (unless
   ``--dry-run``), refusing to overwrite without ``--force``.
5. Registers the filenames in ``recipe_list.json``, ``recipe_list_assigner.txt``
   and ``recipe_list_pruner.txt`` and ensures the recipe repos that the trust
   info references are present in ``repo_list.txt``. All list updates are
   idempotent.

Exit codes: 0 complete, 1 error, 2 partial (an assigner/pruner parent was
missing and ``--allow-partial`` was not given).

Run with ``uv run scripts/override_generator/generate_ws1_overrides.py --help``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import yaml

KINDS = ("importer", "assigner", "pruner")
KIND_SUFFIX = {"importer": "ws1", "assigner": "ws1-assigner", "pruner": "ws1-pruner"}
KIND_LIST_FILE = {
    "importer": "recipe_list.json",
    "assigner": "recipe_list_assigner.txt",
    "pruner": "recipe_list_pruner.txt",
}
UPSTREAM_REPO_URL = "https://github.com/autopkg/WorkSpaceOneImporter-recipes"
UPSTREAM_REPO_DIRNAME = "com.github.autopkg.WorkSpaceOneImporter-recipes"
PLACEHOLDER_RE = re.compile(r"^[A-Z0-9_]+_HERE$")
NAME_TOKEN = "{NAME}"
DEPRECATED_DIR = "ws1-recipes-deprecated"

HERE = Path(__file__).resolve().parent
DEFAULT_TEMPLATE = HERE / "templates" / "overrides_defaults.yaml"
LOCAL_TEMPLATE = HERE / "templates" / "overrides_defaults.local.yaml"
GENERIC_TEMPLATE_NOTICE = (
    f"NOTICE: using the generic defaults template {DEFAULT_TEMPLATE.name} (placeholder GROUP-* smart "
    f"group names). For production, create {LOCAL_TEMPLATE.name} next to it with your organisation's real "
    "smart group names; it is picked up automatically."
)

RepoListRunner = Callable[[], str]
MakeOverrideRunner = Callable[[str, str, Path], Path]


class GeneratorError(Exception):
    """Fatal, user-facing error."""


@dataclass
class UpstreamRecipe:
    kind: str
    path: Path
    identifier: str
    name_input: str | None


@dataclass
class Discovery:
    stem: str
    recipes: dict[str, UpstreamRecipe] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)


@dataclass
class KindResult:
    kind: str
    status: str  # created | would-create | skipped-exists | missing-parent | failed
    path: Path | None = None
    detail: str = ""
    data: dict | None = None


# --------------------------------------------------------------------------- yaml


class _LiteralDumper(yaml.SafeDumper):
    pass


def _str_representer(dumper: yaml.SafeDumper, value: str):
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_LiteralDumper.add_representer(str, _str_representer)


def dump_yaml(data: dict) -> str:
    return yaml.dump(
        data,
        Dumper=_LiteralDumper,
        sort_keys=True,
        allow_unicode=True,
        default_flow_style=False,
    )


def load_yaml_file(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as err:
        raise GeneratorError(f"Invalid YAML in {path}: {err}") from err
    if not isinstance(data, dict):
        raise GeneratorError(f"Expected a mapping at top level of {path}")
    return data


# ---------------------------------------------------------------------- discovery


def _iter_recipe_files(source_repo: Path) -> Iterable[Path]:
    for path in sorted(source_repo.rglob("*.recipe.yaml")):
        if DEPRECATED_DIR in path.parts or ".git" in path.parts:
            continue
        yield path


def _kind_from_filename(filename: str) -> str | None:
    for kind, suffix in KIND_SUFFIX.items():
        if filename.endswith(f".{suffix}.recipe.yaml"):
            return kind
    return None


def _stem_from_filename(filename: str, kind: str) -> str:
    return filename[: -len(f".{KIND_SUFFIX[kind]}.recipe.yaml")]


def discover_upstream(source_repo: Path, spec: str) -> Discovery:
    """Locate the upstream importer and its assigner/pruner siblings.

    ``spec`` may be an app stem (``PolyStudio``), an upstream filename
    (``PolyStudio.ws1.recipe.yaml``) or an importer identifier
    (``com.github.codeskipper.ws1.PolyStudio``).
    """
    if not source_repo.is_dir():
        raise GeneratorError(
            f"Source repo not found: {source_repo}. Run `autopkg repo-add {UPSTREAM_REPO_URL}` "
            "or pass --source-repo."
        )

    by_kind: dict[str, list[UpstreamRecipe]] = {kind: [] for kind in KINDS}
    for path in _iter_recipe_files(source_repo):
        kind = _kind_from_filename(path.name)
        if kind is None:
            continue
        try:
            data = load_yaml_file(path)
        except GeneratorError:
            continue
        identifier = data.get("Identifier")
        if not isinstance(identifier, str) or not identifier:
            continue
        name_input = (data.get("Input") or {}).get("NAME")
        by_kind[kind].append(UpstreamRecipe(kind, path, identifier, name_input))

    spec = spec.strip()
    if spec.endswith(".recipe.yaml"):
        kind = _kind_from_filename(spec)
        if kind != "importer":
            raise GeneratorError(
                f"Expected an importer recipe filename (*.ws1.recipe.yaml), got {spec}"
            )
        spec = _stem_from_filename(spec, "importer")

    importers = [
        r
        for r in by_kind["importer"]
        if r.identifier == spec or _stem_from_filename(r.path.name, "importer") == spec
    ]
    if not importers:
        raise GeneratorError(
            f"No upstream importer recipe matching '{spec}' found under {source_repo}"
        )
    if len(importers) > 1:
        listing = "\n  ".join(str(r.path) for r in importers)
        raise GeneratorError(f"Ambiguous importer match for '{spec}':\n  {listing}")
    importer = importers[0]
    stem = _stem_from_filename(importer.path.name, "importer")
    id_suffix = importer.identifier.rsplit(".", 1)[-1]

    discovery = Discovery(stem=stem, recipes={"importer": importer})
    for kind in ("assigner", "pruner"):
        expected_identifier = (
            f"{importer.identifier.rsplit('.', 2)[0]}.{KIND_SUFFIX[kind]}.{id_suffix}"
        )
        candidates = [
            r
            for r in by_kind[kind]
            if r.identifier == expected_identifier
            or _stem_from_filename(r.path.name, kind) == stem
        ]
        if not candidates:
            discovery.missing.append(kind)
            continue
        if len(candidates) > 1:
            listing = "\n  ".join(str(r.path) for r in candidates)
            raise GeneratorError(f"Ambiguous {kind} match for '{stem}':\n  {listing}")
        discovery.recipes[kind] = candidates[0]
    return discovery


# ------------------------------------------------------------------ make-override


def default_make_override_runner(
    prefs: Path | None, autopkg_bin: str
) -> MakeOverrideRunner:
    def run(recipe_identifier: str, override_name: str, out_dir: Path) -> Path:
        if shutil.which(autopkg_bin) is None and not Path(autopkg_bin).exists():
            raise GeneratorError(
                f"autopkg not found at '{autopkg_bin}'. Install AutoPkg or pass --autopkg-bin."
            )
        cmd = [
            autopkg_bin,
            "make-override",
            "--format=yaml",
            f"--override-dir={out_dir}",
            f"--name={override_name}",
        ]
        if prefs is not None:
            cmd.append(f"--prefs={prefs}")
        cmd.append(recipe_identifier)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        expected = out_dir / f"{override_name}.recipe.yaml"
        if proc.returncode != 0 or not expected.exists():
            raise GeneratorError(
                f"`{' '.join(cmd)}` failed (exit {proc.returncode}):\n{proc.stdout.strip()}\n{proc.stderr.strip()}"
            )
        return expected

    return run


def default_repo_list_runner(prefs: Path | None, autopkg_bin: str) -> RepoListRunner:
    def run() -> str:
        cmd = [autopkg_bin, "repo-list"]
        if prefs is not None:
            cmd.append(f"--prefs={prefs}")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        return proc.stdout if proc.returncode == 0 else ""

    return run


# ------------------------------------------------------------------ post-process


def resolve_template(
    explicit: Path | None,
    local_template: Path = LOCAL_TEMPLATE,
    default_template: Path = DEFAULT_TEMPLATE,
) -> tuple[Path, str | None]:
    """Pick the defaults template: explicit ``--template`` > local > generic (with notice)."""
    if explicit is not None:
        return explicit, None
    if local_template.exists():
        return local_template, None
    return default_template, GENERIC_TEMPLATE_NOTICE


def load_defaults(template_path: Path) -> dict:
    data = load_yaml_file(template_path)
    for kind in KINDS:
        section = data.get(kind)
        if section is None:
            data[kind] = {"Input": {}}
        elif not isinstance(section, dict) or not isinstance(
            section.get("Input", {}), dict
        ):
            raise GeneratorError(
                f"Template section '{kind}' must be a mapping with an 'Input' mapping: {template_path}"
            )
        else:
            section.setdefault("Input", {})
    return data


def _substitute_name(value, name: str):
    if isinstance(value, str):
        return value.replace(NAME_TOKEN, name)
    if isinstance(value, list):
        return [_substitute_name(v, name) for v in value]
    if isinstance(value, dict):
        return {k: _substitute_name(v, name) for k, v in value.items()}
    return value


def parse_set_options(values: Iterable[str]) -> dict[str, dict[str, str]]:
    """Parse ``[kind:]KEY=VALUE`` into ``{kind: {KEY: VALUE}}`` (kind ``*`` = all)."""
    result: dict[str, dict[str, str]] = {"*": {}, **{kind: {} for kind in KINDS}}
    for item in values:
        if "=" not in item:
            raise GeneratorError(f"--set expects [kind:]KEY=VALUE, got '{item}'")
        key, value = item.split("=", 1)
        kind = "*"
        if ":" in key:
            kind, key = key.split(":", 1)
            if kind not in KINDS:
                raise GeneratorError(
                    f"--set kind must be one of {', '.join(KINDS)}, got '{kind}'"
                )
        if not key:
            raise GeneratorError(f"--set has empty key: '{item}'")
        result[kind][key] = value
    return result


def post_process(
    data: dict,
    kind: str,
    defaults: dict,
    set_options: dict[str, dict[str, str]],
    warnings: list[str] | None = None,
) -> dict:
    """Apply project defaults to an ``autopkg make-override`` result.

    Template keys that the parent recipe does not define are still applied (the
    template is repo policy) but reported via ``warnings`` so a stale key name,
    e.g. after an upstream rename, is noticed instead of silently shipped.
    """
    inputs = data.get("Input")
    if not isinstance(inputs, dict):
        raise GeneratorError(f"Generated {kind} override has no Input mapping")

    for key, value in list(inputs.items()):
        if isinstance(value, str) and PLACEHOLDER_RE.match(value):
            inputs[key] = "none"

    name = inputs.get("NAME") if isinstance(inputs.get("NAME"), str) else ""
    overlay = _substitute_name(defaults[kind]["Input"], name)
    for key in overlay:
        if key.startswith("WS1_") and key not in inputs and warnings is not None:
            warnings.append(
                f"{kind}: template sets {key}, which the upstream recipe does not define; "
                "check for a renamed/deprecated input"
            )
    inputs.update(overlay)
    inputs.update(set_options.get("*", {}))
    inputs.update(set_options.get(kind, {}))

    if kind == "pruner" and "WS1_APP_VERSIONS_TO_KEEP" in inputs:
        inputs["WS1_APP_VERSIONS_TO_KEEP"] = str(inputs["WS1_APP_VERSIONS_TO_KEEP"])
    return data


def check_sibling_consistency(
    overrides_dir: Path, local_name: str, discovery: Discovery, kinds: Iterable[str]
) -> list[str]:
    """Refuse to extend a set whose existing members disagree with what we would generate.

    Mirrors the upstream recipe scaffolder: an existing ``<Name>.<kind>`` override must have
    the expected Identifier, the expected upstream ParentRecipe, and the same NAME as the
    upstream importer. Returns a list of problems (empty = consistent).
    """
    problems: list[str] = []
    expected_name = discovery.recipes["importer"].name_input
    for kind in KINDS:
        path = overrides_dir / f"{local_name}.{KIND_SUFFIX[kind]}.recipe.yaml"
        if not path.exists():
            continue
        try:
            existing = load_yaml_file(path)
        except GeneratorError as err:
            problems.append(str(err))
            continue
        expected_identifier = f"local.{KIND_SUFFIX[kind]}.{local_name}"
        if existing.get("Identifier") != expected_identifier:
            problems.append(
                f"{path.name}: Identifier {existing.get('Identifier')!r} != {expected_identifier!r}"
            )
        upstream = discovery.recipes.get(kind)
        if upstream is not None and existing.get("ParentRecipe") != upstream.identifier:
            problems.append(
                f"{path.name}: ParentRecipe {existing.get('ParentRecipe')!r} != {upstream.identifier!r}"
            )
        existing_name = (existing.get("Input") or {}).get("NAME")
        if expected_name and existing_name != expected_name:
            problems.append(
                f"{path.name}: NAME {existing_name!r} != upstream {expected_name!r}"
            )
    return problems


def validate_override(
    data: dict, kind: str, local_name: str, upstream: UpstreamRecipe
) -> list[str]:
    problems: list[str] = []
    expected_identifier = f"local.{KIND_SUFFIX[kind]}.{local_name}"
    if data.get("Identifier") != expected_identifier:
        problems.append(
            f"Identifier is {data.get('Identifier')!r}, expected {expected_identifier!r}"
        )
    if data.get("ParentRecipe") != upstream.identifier:
        problems.append(
            f"ParentRecipe is {data.get('ParentRecipe')!r}, expected {upstream.identifier!r}"
        )
    inputs = data.get("Input")
    if not isinstance(inputs, dict):
        problems.append("Input is not a mapping")
        inputs = {}
    for key, value in inputs.items():
        if isinstance(value, str) and PLACEHOLDER_RE.match(value):
            problems.append(f"Input {key} still has upstream placeholder {value!r}")
    trust = data.get("ParentRecipeTrustInfo")
    if not isinstance(trust, dict) or not trust.get("parent_recipes"):
        problems.append("ParentRecipeTrustInfo.parent_recipes is missing or empty")
    if kind == "pruner":
        keep = inputs.get("WS1_APP_VERSIONS_TO_KEEP")
        if not isinstance(keep, str) or not keep.isdigit():
            problems.append(
                f"WS1_APP_VERSIONS_TO_KEEP must be a numeric string, got {keep!r}"
            )
    if kind == "assigner" and not inputs.get("ws1_app_assignments"):
        problems.append("ws1_app_assignments is missing or empty")
    return problems


# ------------------------------------------------------------------ registration


def _normalise_url(url: str) -> str:
    return url.strip().rstrip("/").removesuffix(".git").lower()


def parse_repo_list(output: str) -> dict[str, str]:
    """Map local repo directory path -> URL from ``autopkg repo-list`` output."""
    mapping: dict[str, str] = {}
    for line in output.splitlines():
        match = re.match(r"^(?P<path>\S.*?) \((?P<url>\S+)\)\s*$", line.strip())
        if match:
            mapping[os.path.expanduser(match.group("path")).rstrip("/")] = match.group(
                "url"
            )
    return mapping


def repo_urls_from_trust_info(
    trust: dict, repo_map: dict[str, str]
) -> tuple[set[str], set[str]]:
    """Return (resolved URLs, unresolved repo dirs) for every path in trust info."""
    resolved: set[str] = set()
    unresolved: set[str] = set()
    for section in ("parent_recipes", "non_core_processors"):
        for entry in (trust.get(section) or {}).values():
            path = entry.get("path") if isinstance(entry, dict) else None
            if not isinstance(path, str):
                continue
            path = os.path.expanduser(path)
            repo_dir = next((d for d in repo_map if path.startswith(d + os.sep)), None)
            if repo_dir:
                resolved.add(repo_map[repo_dir])
                continue
            match = re.search(r"/repos/([^/]+)/", path)
            if match:
                unresolved.add(match.group(1))
    return resolved, unresolved


def add_to_json_list(path: Path, entry: str, dry_run: bool) -> bool:
    try:
        entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    except json.JSONDecodeError as err:
        raise GeneratorError(f"Invalid JSON in {path}: {err}") from err
    if not isinstance(entries, list):
        raise GeneratorError(f"{path} must contain a JSON array")
    if entry in entries:
        return False
    if not dry_run:
        entries.append(entry)
        path.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
    return True


def add_to_text_list(
    path: Path, entry: str, dry_run: bool, normalise=lambda s: s.strip()
) -> bool:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    if any(normalise(line) == normalise(entry) for line in lines if line.strip()):
        return False
    if not dry_run:
        text = "\n".join(lines).rstrip("\n")
        path.write_text((text + "\n" if text else "") + entry + "\n", encoding="utf-8")
    return True


# -------------------------------------------------------------------------- main


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  uv run scripts/override_generator/generate_ws1_overrides.py PolyStudio --dry-run\n"
            "  uv run scripts/override_generator/generate_ws1_overrides.py PolyStudio\n"
            "  uv run scripts/override_generator/generate_ws1_overrides.py DockerDesktop \\\n"
            "      --name DockerDesktop-Intel --set importer:PLATFORM_ARCH=amd64\n"
            "  uv run scripts/override_generator/generate_ws1_overrides.py PolyStudio \\\n"
            "      --set pruner:WS1_APP_VERSIONS_TO_KEEP=12\n"
        ),
    )
    parser.add_argument(
        "recipe", help="upstream app stem, importer filename or importer Identifier"
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--source-repo",
        type=Path,
        help=f"upstream checkout (default: autopkg/repos/{UPSTREAM_REPO_DIRNAME})",
    )
    parser.add_argument(
        "--name",
        help="local override base name (default: upstream stem), e.g. DockerDesktop-Intel",
    )
    parser.add_argument(
        "--kinds",
        default=",".join(KINDS),
        help=f"comma-separated subset of {','.join(KINDS)}",
    )
    parser.add_argument(
        "--template",
        type=Path,
        default=None,
        help=f"defaults template YAML (default: {LOCAL_TEMPLATE.name} if present, else {DEFAULT_TEMPLATE.name})",
    )
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="[KIND:]KEY=VALUE",
        help="extra/override Input value",
    )
    parser.add_argument(
        "--prefs",
        type=Path,
        help="AutoPkg prefs file (default: autopkg/autopkg_prefs.plist if present)",
    )
    parser.add_argument(
        "--autopkg-bin",
        default="autopkg",
        help="autopkg executable (default: autopkg on PATH)",
    )
    parser.add_argument("--dry-run", action="store_true", help="preview; write nothing")
    parser.add_argument(
        "--force", action="store_true", help="overwrite existing override files"
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="exit 0 even if assigner/pruner parent is missing",
    )
    parser.add_argument(
        "--no-register",
        action="store_true",
        help="do not touch recipe lists or repo_list.txt",
    )
    return parser.parse_args(argv)


def generate(
    args: argparse.Namespace,
    make_override: MakeOverrideRunner,
    repo_list: RepoListRunner,
    out=sys.stdout,
) -> int:
    repo_root: Path = args.repo_root.resolve()
    autopkg_dir = repo_root / "autopkg"
    overrides_dir = autopkg_dir / "overrides"
    source_repo = (
        args.source_repo or autopkg_dir / "repos" / UPSTREAM_REPO_DIRNAME
    ).resolve()

    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    unknown = [k for k in kinds if k not in KINDS]
    if unknown or not kinds:
        raise GeneratorError(
            f"--kinds must be a non-empty subset of {', '.join(KINDS)}"
        )

    template_path, template_notice = resolve_template(args.template)
    if template_notice:
        print(template_notice, file=out)
    defaults = load_defaults(template_path)
    set_options = parse_set_options(args.set)
    discovery = discover_upstream(source_repo, args.recipe)
    local_name = args.name or discovery.stem
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", local_name):
        raise GeneratorError(f"--name must be a safe filename stem, got '{local_name}'")

    print(
        f"Upstream importer: {discovery.recipes['importer'].path} ({discovery.recipes['importer'].identifier})",
        file=out,
    )
    print(f"Local name: {local_name}{'   [DRY RUN]' if args.dry_run else ''}", file=out)

    sibling_problems = check_sibling_consistency(
        overrides_dir, local_name, discovery, kinds
    )
    if sibling_problems and not args.force:
        raise GeneratorError(
            "Existing overrides for this name are inconsistent with the upstream recipe set; "
            "review them before generating related overrides (or use --force to regenerate):\n  "
            + "\n  ".join(sibling_problems)
        )

    warnings: list[str] = []
    results: list[KindResult] = []
    with tempfile.TemporaryDirectory(prefix="ws1-override-") as tmp:
        tmp_dir = Path(tmp)
        for kind in kinds:
            upstream = discovery.recipes.get(kind)
            override_name = f"{local_name}.{KIND_SUFFIX[kind]}"
            target = overrides_dir / f"{override_name}.recipe.yaml"
            if upstream is None:
                results.append(
                    KindResult(
                        kind,
                        "missing-parent",
                        target,
                        f"no upstream {KIND_SUFFIX[kind]} recipe for '{discovery.stem}'",
                    )
                )
                continue
            if target.exists() and not args.force:
                results.append(
                    KindResult(
                        kind,
                        "skipped-exists",
                        target,
                        "exists; use --force to overwrite",
                    )
                )
                continue
            try:
                generated = make_override(upstream.identifier, override_name, tmp_dir)
                data = post_process(
                    load_yaml_file(generated), kind, defaults, set_options, warnings
                )
            except GeneratorError as err:
                results.append(KindResult(kind, "failed", target, str(err)))
                continue
            problems = validate_override(data, kind, local_name, upstream)
            if problems:
                results.append(
                    KindResult(
                        kind,
                        "failed",
                        target,
                        "validation: " + "; ".join(problems),
                        data,
                    )
                )
                continue
            if not args.dry_run:
                overrides_dir.mkdir(parents=True, exist_ok=True)
                target.write_text(dump_yaml(data), encoding="utf-8")
            results.append(
                KindResult(
                    kind,
                    "created" if not args.dry_run else "would-create",
                    target,
                    "",
                    data,
                )
            )

    print("", file=out)
    for res in results:
        print(
            f"[{res.status:>14}] {res.kind:<8} {res.path}"
            + (f"  -- {res.detail}" if res.detail else ""),
            file=out,
        )
        if args.dry_run and res.data is not None and res.status == "would-create":
            print("  " + dump_yaml(res.data).replace("\n", "\n  ").rstrip(), file=out)

    # Registration ------------------------------------------------------------
    registered_any = False
    if not args.no_register:
        repo_map = parse_repo_list(repo_list())
        required_urls: set[str] = set()
        unresolved: set[str] = set()
        for res in results:
            if res.status not in ("created", "would-create", "skipped-exists"):
                continue
            list_path = autopkg_dir / KIND_LIST_FILE[res.kind]
            filename = res.path.name
            if res.kind == "importer":
                changed = add_to_json_list(list_path, filename, args.dry_run)
            else:
                changed = add_to_text_list(list_path, filename, args.dry_run)
            registered_any |= changed
            verb = "would add" if args.dry_run else "added"
            print(
                f"{list_path.name}: {verb + ' ' + filename if changed else 'already lists ' + filename}",
                file=out,
            )
            trust = (res.data or {}).get("ParentRecipeTrustInfo") or {}
            urls, missing = repo_urls_from_trust_info(trust, repo_map)
            required_urls |= urls
            unresolved |= missing

        required_urls.add(UPSTREAM_REPO_URL)
        repo_list_path = autopkg_dir / "repo_list.txt"
        for url in sorted(required_urls):
            changed = add_to_text_list(
                repo_list_path, url, args.dry_run, normalise=_normalise_url
            )
            registered_any |= changed
            if changed:
                print(
                    f"repo_list.txt: {'would add' if args.dry_run else 'added'} {url}",
                    file=out,
                )
        if unresolved:
            print(
                "WARNING: could not map these recipe repo dirs to URLs (autopkg repo-list unavailable?); "
                "verify repo_list.txt manually: " + ", ".join(sorted(unresolved)),
                file=out,
            )

    # Summary -----------------------------------------------------------------
    for warning in warnings:
        print(f"WARNING: {warning}", file=out)
    failed = [r for r in results if r.status == "failed"]
    missing = [r for r in results if r.status == "missing-parent"]
    print("", file=out)
    if failed:
        print(
            "RESULT: FAILED -- " + "; ".join(f"{r.kind}: {r.detail}" for r in failed),
            file=out,
        )
        return 1
    if missing:
        print(
            "RESULT: PARTIAL -- missing upstream recipe(s): "
            + ", ".join(r.kind for r in missing)
            + ". The upstream repo has no such recipe; add one upstream or run with --allow-partial.",
            file=out,
        )
        return 0 if args.allow_partial else 2
    print(
        "RESULT: COMPLETE" + (" (dry run, nothing written)" if args.dry_run else ""),
        file=out,
    )
    if not args.dry_run:
        print(
            "Next: review the generated overrides (smart groups, delays, retention), then verify trust with\n"
            f"  autopkg verify-trust-info --prefs autopkg/autopkg_prefs.plist {local_name}.ws1.recipe.yaml",
            file=out,
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        prefs = args.prefs
        if prefs is None:
            candidate = args.repo_root.resolve() / "autopkg" / "autopkg_prefs.plist"
            prefs = candidate if candidate.exists() else None
        elif not prefs.exists():
            raise GeneratorError(f"--prefs file not found: {prefs}")
        return generate(
            args,
            make_override=default_make_override_runner(prefs, args.autopkg_bin),
            repo_list=default_repo_list_runner(prefs, args.autopkg_bin),
        )
    except GeneratorError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
