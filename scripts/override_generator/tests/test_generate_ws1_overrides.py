"""Unit tests for generate_ws1_overrides.py (no AutoPkg or WS1 access required).

Run: uv run python -m unittest discover -s scripts/override_generator/tests -v
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import generate_ws1_overrides as gen  # noqa: E402

UPSTREAM_ID_BASE = "com.github.codeskipper"


def upstream_recipe(
    kind: str, app: str, name: str, extra_inputs: dict | None = None
) -> dict:
    suffix = gen.KIND_SUFFIX[kind]
    inputs = {
        "NAME": name,
        "WS1_OAUTH_CLIENT_ID": "OAUTH2_CLIENT_ID_HERE",
        "WS1_API_URL": "WORKSPACEONE_API_URL_HERE",
        "WS1_GROUPID": "GROUP_ID_HERE",
    }
    if kind == "pruner":
        inputs["WS1_APP_VERSIONS_TO_KEEP"] = "NUMBER_OF_VERSIONS_TO_KEEP_HERE"
        inputs["WS1_PRUNE_SAFEGUARD_HALT_ON_ASSIGNED"] = "1"
    inputs.update(extra_inputs or {})
    recipe = {
        "Identifier": f"{UPSTREAM_ID_BASE}.{suffix}.{app}",
        "Input": inputs,
        "Process": [],
    }
    if kind == "importer":
        recipe["ParentRecipe"] = f"com.github.example.munki.{app}"
    return recipe


class Fixture:
    """Temporary repo layout mirroring autopkg-cicd."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ws1gen-test-")
        self.root = Path(self.tmp.name)
        self.autopkg = self.root / "autopkg"
        self.overrides = self.autopkg / "overrides"
        self.repos = self.autopkg / "repos"
        self.source = self.repos / gen.UPSTREAM_REPO_DIRNAME
        self.recipes_dir = self.source / "ws1-recipes"
        self.recipes_dir.mkdir(parents=True)
        self.overrides.mkdir()
        (self.autopkg / "recipe_list.json").write_text(
            '[\n  "Existing.ws1.recipe.yaml"\n]\n'
        )
        (self.autopkg / "recipe_list_assigner.txt").write_text(
            "Existing.ws1-assigner.recipe.yaml\n"
        )
        (self.autopkg / "recipe_list_pruner.txt").write_text(
            "Existing.ws1-pruner.recipe.yaml\n"
        )
        (self.autopkg / "repo_list.txt").write_text(
            "https://github.com/autopkg/recipes\n" + gen.UPSTREAM_REPO_URL + "\n"
        )
        self.munki_repo_dir = self.repos / "com.github.autopkg.example-recipes"
        self.munki_repo_dir.mkdir()
        self.make_override_calls: list[tuple[str, str]] = []

    def cleanup(self):
        self.tmp.cleanup()

    def add_upstream(
        self,
        app: str,
        name: str,
        kinds=gen.KINDS,
        extra_inputs=None,
        subdir="ws1-recipes",
    ):
        for kind in kinds:
            target_dir = self.source / subdir
            target_dir.mkdir(parents=True, exist_ok=True)
            path = target_dir / f"{app}.{gen.KIND_SUFFIX[kind]}.recipe.yaml"
            path.write_text(
                yaml.safe_dump(upstream_recipe(kind, app, name, extra_inputs))
            )

    def fake_make_override(
        self, recipe_identifier: str, override_name: str, out_dir: Path
    ) -> Path:
        """Mimic `autopkg make-override`: copy Input, reverse the name for the Identifier, add trust info."""
        self.make_override_calls.append((recipe_identifier, override_name))
        match = None
        for path in self.source.rglob("*.recipe.yaml"):
            data = yaml.safe_load(path.read_text())
            if data.get("Identifier") == recipe_identifier:
                match = (path, data)
                break
        if match is None:
            raise gen.GeneratorError(f"No valid recipe found for {recipe_identifier}")
        path, data = match
        reversed_name = ".".join(reversed(override_name.split(".")))
        trust = {
            "non_core_processors": {},
            "parent_recipes": {
                recipe_identifier: {
                    "git_hash": "abc",
                    "path": str(path),
                    "sha256_hash": "def",
                },
            },
        }
        if "ParentRecipe" in data:
            trust["parent_recipes"][data["ParentRecipe"]] = {
                "git_hash": "123",
                "path": str(self.munki_repo_dir / "App" / "App.munki.recipe"),
                "sha256_hash": "456",
            }
        override = {
            "Identifier": f"local.{reversed_name}",
            "Input": dict(data["Input"]),
            "ParentRecipe": recipe_identifier,
            "ParentRecipeTrustInfo": trust,
        }
        out = out_dir / f"{override_name}.recipe.yaml"
        out.write_text(yaml.safe_dump(override))
        return out

    def fake_repo_list(self) -> str:
        return (
            f"{self.source} ({gen.UPSTREAM_REPO_URL})\n"
            f"{self.munki_repo_dir} (https://github.com/autopkg/example-recipes.git)\n"
        )

    def args(self, recipe: str, **overrides) -> argparse.Namespace:
        # Pin the generic template so results do not depend on a .local.yaml on the dev machine.
        overrides.setdefault("template", gen.DEFAULT_TEMPLATE)
        argv = [recipe, "--repo-root", str(self.root)]
        for key, value in overrides.items():
            flag = "--" + key.replace("_", "-")
            if value is True:
                argv.append(flag)
            elif isinstance(value, list):
                for item in value:
                    argv.extend([flag, item])
            elif value is not None and value is not False:
                argv.extend([flag, str(value)])
        return gen.parse_args(argv)

    def run(self, recipe: str, **overrides) -> tuple[int, str]:
        out = io.StringIO()
        code = gen.generate(
            self.args(recipe, **overrides),
            self.fake_make_override,
            self.fake_repo_list,
            out=out,
        )
        return code, out.getvalue()


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.cleanup)
        self.fx.add_upstream("PolyStudio", "HP Poly Studio App")

    def test_discover_by_stem_filename_and_identifier(self):
        for spec in (
            "PolyStudio",
            "PolyStudio.ws1.recipe.yaml",
            f"{UPSTREAM_ID_BASE}.ws1.PolyStudio",
        ):
            disc = gen.discover_upstream(self.fx.source, spec)
            self.assertEqual(disc.stem, "PolyStudio")
            self.assertEqual(set(disc.recipes), set(gen.KINDS))
            self.assertEqual(disc.missing, [])

    def test_missing_pruner_is_reported_not_guessed(self):
        self.fx.add_upstream("Solo", "Solo App", kinds=("importer", "assigner"))
        disc = gen.discover_upstream(self.fx.source, "Solo")
        self.assertEqual(disc.missing, ["pruner"])
        self.assertNotIn("pruner", disc.recipes)

    def test_identifier_suffix_differs_from_filename_stem(self):
        # Firefox.ws1.recipe.yaml has Identifier ...ws1.MozillaFirefox upstream.
        for kind in gen.KINDS:
            data = upstream_recipe(kind, "MozillaFirefox", "Mozilla Firefox")
            (
                self.fx.recipes_dir / f"Firefox.{gen.KIND_SUFFIX[kind]}.recipe.yaml"
            ).write_text(yaml.safe_dump(data))
        disc = gen.discover_upstream(self.fx.source, "Firefox")
        self.assertEqual(
            disc.recipes["assigner"].identifier,
            f"{UPSTREAM_ID_BASE}.ws1-assigner.MozillaFirefox",
        )

    def test_unknown_recipe_errors(self):
        with self.assertRaises(gen.GeneratorError):
            gen.discover_upstream(self.fx.source, "Nope")

    def test_non_importer_filename_rejected(self):
        with self.assertRaises(gen.GeneratorError):
            gen.discover_upstream(self.fx.source, "PolyStudio.ws1-assigner.recipe.yaml")

    def test_ambiguous_match_errors(self):
        self.fx.add_upstream(
            "PolyStudio",
            "HP Poly Studio App",
            kinds=("importer",),
            subdir="ws1-recipes-plist",
        )
        with self.assertRaises(gen.GeneratorError) as ctx:
            gen.discover_upstream(self.fx.source, "PolyStudio")
        self.assertIn("Ambiguous", str(ctx.exception))

    def test_deprecated_dir_ignored(self):
        self.fx.add_upstream("OldApp", "Old", subdir=gen.DEPRECATED_DIR)
        with self.assertRaises(gen.GeneratorError):
            gen.discover_upstream(self.fx.source, "OldApp")

    def test_missing_source_repo_errors(self):
        with self.assertRaises(gen.GeneratorError):
            gen.discover_upstream(self.fx.root / "nowhere", "PolyStudio")


class PostProcessTests(unittest.TestCase):
    def setUp(self):
        self.defaults = gen.load_defaults(gen.DEFAULT_TEMPLATE)

    def test_placeholders_become_none_and_overlay_applied(self):
        data = {
            "Input": {
                "NAME": "App",
                "WS1_API_URL": "WORKSPACEONE_API_URL_HERE",
                "KEEP": "value",
            }
        }
        out = gen.post_process(
            data, "importer", self.defaults, gen.parse_set_options([])
        )
        self.assertEqual(out["Input"]["WS1_API_URL"], "none")
        self.assertEqual(out["Input"]["KEEP"], "value")

    def test_assigner_gets_assignments_with_name_substitution(self):
        defaults = {k: {"Input": {}} for k in gen.KINDS}
        defaults["assigner"]["Input"] = {
            "ws1_app_assignments": [{"distribution": {"description": "Deploy {NAME}"}}]
        }
        data = {"Input": {"NAME": "HP Poly Studio App"}}
        out = gen.post_process(data, "assigner", defaults, gen.parse_set_options([]))
        self.assertEqual(
            out["Input"]["ws1_app_assignments"][0]["distribution"]["description"],
            "Deploy HP Poly Studio App",
        )

    def test_pruner_retention_is_string(self):
        data = {
            "Input": {"WS1_APP_VERSIONS_TO_KEEP": "NUMBER_OF_VERSIONS_TO_KEEP_HERE"}
        }
        out = gen.post_process(
            data,
            "pruner",
            self.defaults,
            gen.parse_set_options(["pruner:WS1_APP_VERSIONS_TO_KEEP=12"]),
        )
        self.assertEqual(out["Input"]["WS1_APP_VERSIONS_TO_KEEP"], "12")
        self.assertIsInstance(out["Input"]["WS1_APP_VERSIONS_TO_KEEP"], str)

    def test_template_resolution_prefers_explicit_then_local_then_generic(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "overrides_defaults.local.yaml"
            explicit = Path(tmp) / "custom.yaml"
            self.assertEqual(
                gen.resolve_template(explicit, local, gen.DEFAULT_TEMPLATE),
                (explicit, None),
            )
            path, notice = gen.resolve_template(None, local, gen.DEFAULT_TEMPLATE)
            self.assertEqual(path, gen.DEFAULT_TEMPLATE)
            self.assertIn("overrides_defaults.local.yaml", notice)
            local.write_text("assigner:\n  Input: {}\n")
            self.assertEqual(
                gen.resolve_template(None, local, gen.DEFAULT_TEMPLATE), (local, None)
            )

    def test_generic_template_has_no_real_smart_group_names(self):
        text = gen.DEFAULT_TEMPLATE.read_text()
        self.assertIn("GROUP-TESTING", text)
        self.assertIn("GROUP-PROD", text)
        self.assertNotRegex(text, r"macOS[-_]")

    def test_template_key_missing_upstream_is_applied_with_warning(self):
        data = {"Input": {"NAME": "App", "WS1_APP_VERSIONS_TO_KEEP": "X_HERE"}}
        warnings: list[str] = []
        out = gen.post_process(
            data, "pruner", self.defaults, gen.parse_set_options([]), warnings
        )
        self.assertEqual(out["Input"]["WS1_PRUNE_SAFEGUARD_HALT_ON_ASSIGNED"], "False")
        self.assertEqual(len(warnings), 1)
        self.assertIn("WS1_PRUNE_SAFEGUARD_HALT_ON_ASSIGNED", warnings[0])

    def test_set_options_kind_scoping(self):
        opts = gen.parse_set_options(["PLATFORM_ARCH=arm64", "pruner:X=1"])
        self.assertEqual(opts["*"], {"PLATFORM_ARCH": "arm64"})
        self.assertEqual(opts["pruner"], {"X": "1"})
        for bad in ("novalue", "bogus:K=V", "=v"):
            with self.assertRaises(gen.GeneratorError):
                gen.parse_set_options([bad])

    def test_validation_catches_leftover_placeholder_and_bad_identifier(self):
        upstream = gen.UpstreamRecipe("importer", Path("x"), "com.x.ws1.App", "App")
        data = {
            "Identifier": "local.ws1.Other",
            "ParentRecipe": "com.x.ws1.App",
            "Input": {"WS1_GROUPID": "GROUP_ID_HERE"},
            "ParentRecipeTrustInfo": {"parent_recipes": {}},
        }
        problems = gen.validate_override(data, "importer", "App", upstream)
        self.assertTrue(any("Identifier" in p for p in problems))
        self.assertTrue(any("placeholder" in p for p in problems))
        self.assertTrue(any("parent_recipes" in p for p in problems))


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_json_list_idempotent(self):
        path = self.dir / "recipe_list.json"
        path.write_text('["A.ws1.recipe.yaml"]')
        self.assertTrue(gen.add_to_json_list(path, "B.ws1.recipe.yaml", dry_run=False))
        self.assertFalse(gen.add_to_json_list(path, "B.ws1.recipe.yaml", dry_run=False))
        self.assertEqual(
            json.loads(path.read_text()), ["A.ws1.recipe.yaml", "B.ws1.recipe.yaml"]
        )

    def test_json_list_dry_run_does_not_write(self):
        path = self.dir / "recipe_list.json"
        path.write_text("[]")
        self.assertTrue(gen.add_to_json_list(path, "B.ws1.recipe.yaml", dry_run=True))
        self.assertEqual(json.loads(path.read_text()), [])

    def test_text_list_idempotent_and_url_normalised(self):
        path = self.dir / "repo_list.txt"
        path.write_text("https://github.com/autopkg/gerardkok-recipes.git\n")
        self.assertFalse(
            gen.add_to_text_list(
                path,
                "https://github.com/autopkg/gerardkok-recipes",
                False,
                normalise=gen._normalise_url,
            )
        )
        self.assertTrue(
            gen.add_to_text_list(
                path,
                "https://github.com/autopkg/new-recipes",
                False,
                normalise=gen._normalise_url,
            )
        )
        self.assertEqual(
            path.read_text().splitlines()[-1], "https://github.com/autopkg/new-recipes"
        )

    def test_repo_urls_from_trust_info(self):
        repo_map = gen.parse_repo_list(
            "/r/com.github.autopkg.a (https://github.com/autopkg/a)\n"
        )
        trust = {
            "parent_recipes": {
                "x": {"path": "/r/com.github.autopkg.a/App/App.munki.recipe"},
                "y": {"path": "/r/repos/com.github.autopkg.unknown/App.recipe"},
            }
        }
        urls, unresolved = gen.repo_urls_from_trust_info(trust, repo_map)
        self.assertEqual(urls, {"https://github.com/autopkg/a"})
        self.assertEqual(unresolved, {"com.github.autopkg.unknown"})


class GenerateEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.cleanup)
        self.fx.add_upstream("PolyStudio", "HP Poly Studio App")

    def override(self, kind: str, name="PolyStudio") -> dict:
        return yaml.safe_load(
            (
                self.fx.overrides / f"{name}.{gen.KIND_SUFFIX[kind]}.recipe.yaml"
            ).read_text()
        )

    def test_complete_generation_and_registration(self):
        code, out = self.fx.run("PolyStudio")
        self.assertEqual(code, 0, out)
        self.assertIn("RESULT: COMPLETE", out)
        importer, assigner, pruner = (self.override(k) for k in gen.KINDS)
        self.assertEqual(importer["Identifier"], "local.ws1.PolyStudio")
        self.assertEqual(importer["Input"]["WS1_API_URL"], "none")
        self.assertEqual(assigner["Identifier"], "local.ws1-assigner.PolyStudio")
        self.assertEqual(
            assigner["ParentRecipe"], f"{UPSTREAM_ID_BASE}.ws1-assigner.PolyStudio"
        )
        self.assertEqual(len(assigner["Input"]["ws1_app_assignments"]), 3)
        self.assertEqual(pruner["Input"]["WS1_APP_VERSIONS_TO_KEEP"], "5")
        self.assertEqual(
            pruner["Input"]["WS1_PRUNE_SAFEGUARD_HALT_ON_ASSIGNED"], "False"
        )
        self.assertNotIn("WS1_SAFEGUARD_HALT_ON_ASSIGNED", pruner["Input"])
        self.assertNotIn("WARNING:", out)
        self.assertIn("ParentRecipeTrustInfo", importer)

        self.assertEqual(
            json.loads((self.fx.autopkg / "recipe_list.json").read_text()),
            ["Existing.ws1.recipe.yaml", "PolyStudio.ws1.recipe.yaml"],
        )
        self.assertIn(
            "PolyStudio.ws1-assigner.recipe.yaml",
            (self.fx.autopkg / "recipe_list_assigner.txt").read_text(),
        )
        self.assertIn(
            "PolyStudio.ws1-pruner.recipe.yaml",
            (self.fx.autopkg / "recipe_list_pruner.txt").read_text(),
        )
        repo_list = (self.fx.autopkg / "repo_list.txt").read_text().splitlines()
        self.assertIn("https://github.com/autopkg/example-recipes.git", repo_list)
        self.assertEqual(repo_list.count(gen.UPSTREAM_REPO_URL), 1)

    def test_rerun_is_idempotent_and_protects_existing_files(self):
        self.fx.run("PolyStudio")
        pruner_path = self.fx.overrides / "PolyStudio.ws1-pruner.recipe.yaml"
        hand_edited = pruner_path.read_text() + "# hand-edited\n"
        pruner_path.write_text(hand_edited)
        code, out = self.fx.run("PolyStudio")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count("skipped-exists"), 3)
        self.assertEqual(pruner_path.read_text(), hand_edited)
        self.assertEqual(
            json.loads((self.fx.autopkg / "recipe_list.json").read_text()).count(
                "PolyStudio.ws1.recipe.yaml"
            ),
            1,
        )
        self.assertEqual(
            (self.fx.autopkg / "recipe_list_pruner.txt")
            .read_text()
            .count("PolyStudio"),
            1,
        )
        self.assertEqual(
            (self.fx.autopkg / "repo_list.txt").read_text().count("example-recipes"), 1
        )

    def test_inconsistent_sibling_blocks_generation(self):
        (self.fx.overrides / "PolyStudio.ws1.recipe.yaml").write_text(
            "Identifier: local.ws1.PolyStudio\n"
            "ParentRecipe: com.github.someone.ws1.PolyStudio\n"
            "Input:\n  NAME: Something Else\n"
        )
        with self.assertRaises(gen.GeneratorError) as ctx:
            self.fx.run("PolyStudio")
        out = str(ctx.exception)
        self.assertIn("inconsistent", out)
        self.assertIn("ParentRecipe", out)
        self.assertIn("NAME", out)
        self.assertFalse(
            (self.fx.overrides / "PolyStudio.ws1-assigner.recipe.yaml").exists()
        )
        self.assertNotIn(
            "PolyStudio", (self.fx.autopkg / "recipe_list_assigner.txt").read_text()
        )

    def test_consistent_sibling_allows_completing_the_set(self):
        self.fx.run("PolyStudio")
        (self.fx.overrides / "PolyStudio.ws1-assigner.recipe.yaml").unlink()
        code, out = self.fx.run("PolyStudio")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count("skipped-exists"), 2)
        self.assertTrue(
            (self.fx.overrides / "PolyStudio.ws1-assigner.recipe.yaml").exists()
        )

    def test_force_overwrites(self):
        self.fx.run("PolyStudio")
        (self.fx.overrides / "PolyStudio.ws1-pruner.recipe.yaml").write_text(
            "Identifier: hand-edited\n"
        )
        code, out = self.fx.run("PolyStudio", force=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(
            self.override("pruner")["Identifier"], "local.ws1-pruner.PolyStudio"
        )

    def test_dry_run_writes_nothing(self):
        before = {p.name: p.read_text() for p in self.fx.autopkg.glob("*.*")}
        code, out = self.fx.run("PolyStudio", dry_run=True)
        self.assertEqual(code, 0, out)
        self.assertIn("would-create", out)
        self.assertIn("would add PolyStudio.ws1.recipe.yaml", out)
        self.assertEqual(list(self.fx.overrides.iterdir()), [])
        self.assertEqual(
            {p.name: p.read_text() for p in self.fx.autopkg.glob("*.*")}, before
        )

    def test_missing_pruner_is_partial(self):
        self.fx.add_upstream("Solo", "Solo App", kinds=("importer", "assigner"))
        code, out = self.fx.run("Solo")
        self.assertEqual(code, 2, out)
        self.assertIn("RESULT: PARTIAL", out)
        self.assertIn("missing-parent", out)
        self.assertTrue((self.fx.overrides / "Solo.ws1.recipe.yaml").exists())
        self.assertTrue((self.fx.overrides / "Solo.ws1-assigner.recipe.yaml").exists())
        self.assertFalse((self.fx.overrides / "Solo.ws1-pruner.recipe.yaml").exists())
        self.assertNotIn(
            "Solo", (self.fx.autopkg / "recipe_list_pruner.txt").read_text()
        )

        code, out = self.fx.run("Solo", allow_partial=True)
        self.assertEqual(code, 0, out)

    def test_name_variant_and_set_option(self):
        code, out = self.fx.run(
            "PolyStudio", name="PolyStudio-Intel", set=["importer:PLATFORM_ARCH=amd64"]
        )
        self.assertEqual(code, 0, out)
        importer = self.override("importer", name="PolyStudio-Intel")
        self.assertEqual(importer["Identifier"], "local.ws1.PolyStudio-Intel")
        self.assertEqual(importer["Input"]["PLATFORM_ARCH"], "amd64")
        self.assertNotIn(
            "PLATFORM_ARCH", self.override("pruner", name="PolyStudio-Intel")["Input"]
        )
        self.assertIn(
            (f"{UPSTREAM_ID_BASE}.ws1.PolyStudio", "PolyStudio-Intel.ws1"),
            self.fx.make_override_calls,
        )

    def test_kinds_subset(self):
        code, out = self.fx.run("PolyStudio", kinds="pruner")
        self.assertEqual(code, 0, out)
        self.assertEqual(
            [p.name for p in self.fx.overrides.iterdir()],
            ["PolyStudio.ws1-pruner.recipe.yaml"],
        )
        self.assertNotIn(
            "PolyStudio", (self.fx.autopkg / "recipe_list.json").read_text()
        )

    def test_no_register(self):
        code, out = self.fx.run("PolyStudio", no_register=True)
        self.assertEqual(code, 0, out)
        self.assertNotIn(
            "PolyStudio", (self.fx.autopkg / "recipe_list.json").read_text()
        )

    def test_make_override_failure_reported(self):
        def failing(recipe_identifier, override_name, out_dir):
            raise gen.GeneratorError("autopkg exploded")

        out = io.StringIO()
        code = gen.generate(
            self.fx.args("PolyStudio"), failing, self.fx.fake_repo_list, out=out
        )
        self.assertEqual(code, 1)
        self.assertIn("RESULT: FAILED", out.getvalue())
        self.assertIn("autopkg exploded", out.getvalue())
        self.assertEqual(list(self.fx.overrides.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
