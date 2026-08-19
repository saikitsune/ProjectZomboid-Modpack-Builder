import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from pzmodpack.backend import (
    BuildConfig,
    BuildError,
    build_modpack,
    discover_mods,
    rewrite_mod_info,
    validate_mods,
)


class DiscoveryTests(unittest.TestCase):
    def test_discovers_a_workshop_item_and_all_versioned_mod_ids(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            mod = root / "mods" / "ExampleMod"
            (mod / "42").mkdir(parents=True)
            (mod / "mod.info").write_text("name=Example\nid=ExampleB41\n", encoding="utf-8")
            (mod / "42" / "mod.info").write_text(
                "name=Example B42\nid=ExampleB42\nrequire=\\FrameworkB42\n",
                encoding="utf-8",
            )

            discovered = discover_mods([root])

            self.assertEqual(len(discovered), 1)
            self.assertEqual(discovered[0].folder_name, "ExampleMod")
            self.assertEqual(discovered[0].mod_ids, ("ExampleB41", "ExampleB42"))
            self.assertEqual(discovered[0].workshop_id, None)

    def test_discovers_workshop_id_from_an_immutable_snapshot(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            snapshot = Path(temporary_directory) / "2921417999" / "abcdef"
            mod = snapshot / "mods" / "Scalies"
            mod.mkdir(parents=True)
            (mod / "mod.info").write_text("id=Scalies\n", encoding="utf-8")
            (snapshot / "snapshot.json").write_text(
                '{"workshop_id": "2921417999", "sha256": "abcdef"}\n',
                encoding="utf-8",
            )

            discovered = discover_mods([snapshot])

            self.assertEqual(discovered[0].workshop_id, "2921417999")


class RewriteTests(unittest.TestCase):
    def test_namespaces_identity_and_internal_dependency_references(self) -> None:
        original = (
            "name=Dependent\n"
            "id=Dependent\n"
            "require=\\FrameworkB42,ExternalFramework\n"
            "loadModAfter=\\FrameworkB42\n"
            "incompatible=OldConflict\n"
        )

        rewritten = rewrite_mod_info(
            original,
            {
                "Dependent": "SaiPack_Dependent",
                "FrameworkB42": "SaiPack_FrameworkB42",
            },
        )

        self.assertIn("id=SaiPack_Dependent", rewritten)
        self.assertIn("require=\\SaiPack_FrameworkB42,ExternalFramework", rewritten)
        self.assertIn("loadModAfter=\\SaiPack_FrameworkB42", rewritten)
        self.assertIn("incompatible=OldConflict;Dependent", rewritten)


class ValidationTests(unittest.TestCase):
    def test_reports_duplicate_mod_ids_before_building(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for folder in ("First", "Second"):
                mod = root / "mods" / folder
                mod.mkdir(parents=True)
                (mod / "mod.info").write_text(
                    f"name={folder}\nid=DuplicateId\n",
                    encoding="utf-8",
                )

            issues = validate_mods(discover_mods([root]))

            self.assertEqual([issue.code for issue in issues], ["duplicate_mod_id"])
            self.assertIn("First", issues[0].message)
            self.assertIn("Second", issues[0].message)

    def test_reports_requirements_not_in_the_bundle(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            mod = root / "mods" / "Dependent"
            mod.mkdir(parents=True)
            (mod / "mod.info").write_text(
                "name=Dependent\nid=Dependent\nrequire=ExternalFramework\n",
                encoding="utf-8",
            )

            issues = validate_mods(discover_mods([root]))

            self.assertEqual([issue.code for issue in issues], ["external_dependency"])
            self.assertEqual(issues[0].severity, "warning")
            self.assertIn("ExternalFramework", issues[0].message)

    def test_blocks_explicit_incompatibilities_inside_the_bundle(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first = root / "mods" / "First"
            second = root / "mods" / "Second"
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            (first / "mod.info").write_text(
                "name=First\nid=FirstId\nincompatible=SecondId\n",
                encoding="utf-8",
            )
            (second / "mod.info").write_text(
                "name=Second\nid=SecondId\n",
                encoding="utf-8",
            )

            issues = validate_mods(discover_mods([root]))

            conflicts = [issue for issue in issues if issue.code == "bundled_incompatibility"]
            self.assertEqual(len(conflicts), 1)
            self.assertEqual(conflicts[0].severity, "error")
            self.assertIn("FirstId", conflicts[0].message)
            self.assertIn("SecondId", conflicts[0].message)


class BuildTests(unittest.TestCase):
    def test_builds_namespaced_pack_and_reports_hardcoded_id_references(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            framework = source / "mods" / "Framework"
            dependent = source / "mods" / "Dependent"
            framework.mkdir(parents=True)
            (dependent / "42" / "media" / "lua" / "shared").mkdir(parents=True)
            (framework / "mod.info").write_text(
                "name=Framework\nid=FrameworkB42\n",
                encoding="utf-8",
            )
            (dependent / "42" / "mod.info").write_text(
                "name=Dependent\nid=Dependent\nrequire=\\FrameworkB42\n",
                encoding="utf-8",
            )
            (dependent / "42" / "media" / "lua" / "shared" / "check.lua").write_text(
                'if getActivatedMods():contains("FrameworkB42") then return end\n'
                'if getActivatedMods():contains("\\\\FrameworkB42") then return end\n',
                encoding="utf-8",
            )
            output = root / "output"

            report = build_modpack(
                BuildConfig(
                    name="Sai Test Pack",
                    namespace="SaiPack",
                    sources=(source,),
                    output=output,
                    workshop_id="1234567890",
                )
            )

            framework_info = (
                output / "Contents" / "mods" / "SaiPack_Framework" / "mod.info"
            ).read_text(encoding="utf-8")
            dependent_info = (
                output
                / "Contents"
                / "mods"
                / "SaiPack_Dependent"
                / "42"
                / "mod.info"
            ).read_text(encoding="utf-8")
            self.assertIn("id=SaiPack_FrameworkB42", framework_info)
            self.assertIn("id=SaiPack_Dependent", dependent_info)
            self.assertIn("require=\\SaiPack_FrameworkB42", dependent_info)
            self.assertIn("incompatible=Dependent", dependent_info)
            self.assertIn("WorkshopItems=1234567890;", (output / "amp-config.txt").read_text())
            self.assertIn(
                "Mods=SaiPack_FrameworkB42;SaiPack_Dependent;",
                (output / "amp-config.txt").read_text(),
            )
            self.assertTrue((output / "manifest.json").is_file())
            check_lua = (
                output
                / "Contents"
                / "mods"
                / "SaiPack_Dependent"
                / "42"
                / "media"
                / "lua"
                / "shared"
                / "check.lua"
            ).read_text(encoding="utf-8")
            self.assertIn(
                'getActivatedMods():contains("SaiPack_FrameworkB42")',
                check_lua,
            )
            self.assertIn(
                'getActivatedMods():contains("\\\\SaiPack_FrameworkB42")',
                check_lua,
            )
            self.assertEqual(report.warnings, ())

    def test_build_report_preserves_external_dependency_warnings(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            mod = source / "mods" / "Dependent"
            mod.mkdir(parents=True)
            (mod / "mod.info").write_text(
                "id=Dependent\nrequire=ExternalFramework\n",
                encoding="utf-8",
            )

            report = build_modpack(
                BuildConfig(
                    name="Pack",
                    namespace="Pack",
                    sources=(source,),
                    output=root / "output",
                )
            )

            self.assertTrue(
                any("ExternalFramework" in warning for warning in report.warnings)
            )
            manifest = json.loads(
                (root / "output" / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["warning_details"][0]["category"], "metadata")

    def test_internal_namespace_is_not_mislabeled_as_runtime_mod_lookup(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            mod = root / "source" / "mods" / "SeedSeasonIndicator"
            lua = mod / "42" / "media" / "lua" / "client"
            lua.mkdir(parents=True)
            (mod / "mod.info").write_text("id=SeedSeasonIndicator\n", encoding="utf-8")
            (lua / "SeedSeasonIndicator.lua").write_text(
                'local SeedSeasonIndicator = {}\n'
                'PZAPI.ModOptions:create("SeedSeasonIndicator", "Seed Season Indicator")\n'
                'local activatedMods = getActivatedMods()\n'
                'activatedMods:contains("ShowSowingSeasonInTooltip")\n',
                encoding="utf-8",
            )

            build_modpack(
                BuildConfig(
                    name="Pack",
                    namespace="SSP",
                    sources=(root / "source",),
                    output=root / "output",
                )
            )

            manifest = json.loads(
                (root / "output" / "manifest.json").read_text(encoding="utf-8")
            )
            details = [
                detail
                for detail in manifest["warning_details"]
                if "SeedSeasonIndicator" in detail.get("original_ids", [])
            ]
            self.assertTrue(details)
            self.assertEqual(
                {detail["category"] for detail in details},
                {"content_namespace"},
            )

    def test_build_reports_monotonic_progress_until_complete(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            for name in ("First", "Second"):
                mod = source / "mods" / name
                mod.mkdir(parents=True)
                (mod / "mod.info").write_text(f"id={name}\n", encoding="utf-8")
            events: list[tuple[int, int, str]] = []

            build_modpack(
                BuildConfig(
                    name="Pack",
                    namespace="Pack",
                    sources=(source,),
                    output=root / "output",
                ),
                progress=lambda current, total, message: events.append(
                    (current, total, message)
                ),
            )

            self.assertGreater(len(events), 3)
            self.assertEqual(events[-1], (100, 100, "Build complete"))
            self.assertEqual([event[0] for event in events], sorted(event[0] for event in events))
            self.assertTrue(any("Copying" in event[2] for event in events))

    def test_build_copies_preview_and_respects_workshop_visibility(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            mod = source / "mods" / "Example"
            mod.mkdir(parents=True)
            (mod / "mod.info").write_text("id=Example\n", encoding="utf-8")
            preview = root / "preview.png"
            preview.write_bytes(b"png-bytes")
            output = root / "output"

            build_modpack(
                BuildConfig(
                    name="Pack",
                    namespace="Pack",
                    sources=(source,),
                    output=output,
                    preview=preview,
                    visibility=3,
                )
            )

            self.assertEqual((output / "preview.png").read_bytes(), b"png-bytes")
            self.assertIn("visibility=3", (output / "workshop.txt").read_text())

    def test_build_accepts_an_explicit_active_mod_id_for_versioned_mods(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            mod = root / "source" / "mods" / "Example"
            (mod / "42").mkdir(parents=True)
            (mod / "mod.info").write_text("id=ExampleB41\n", encoding="utf-8")
            (mod / "42" / "mod.info").write_text("id=ExampleB42\n", encoding="utf-8")
            dependent = root / "source" / "mods" / "Dependent"
            dependent.mkdir(parents=True)
            (dependent / "mod.info").write_text(
                "id=Dependent\nrequire=ExampleB41\n",
                encoding="utf-8",
            )
            output = root / "output"

            build_modpack(
                BuildConfig(
                    name="Pack",
                    namespace="Pack",
                    sources=(root / "source",),
                    output=output,
                    active_mod_ids={"Example": "ExampleB42"},
                )
            )

            amp_config = (output / "amp-config.txt").read_text()
            self.assertIn("Pack_ExampleB42", amp_config)
            dependent_info = (
                output / "Contents" / "mods" / "Pack_Dependent" / "mod.info"
            ).read_text(encoding="utf-8")
            self.assertIn("require=Pack_ExampleB42", dependent_info)

    def test_applies_runtime_lookup_patches_and_records_them(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            mods = root / "source" / "mods"
            journal = mods / "Skill Recovery Journal"
            beyond = mods / "BeyondTen"
            music = mods / "Talis New Music"
            for folder, mod_id in (
                (journal, "SkillRecoveryJournal"),
                (beyond, "BeyondTen"),
                (music, "NewMusic"),
            ):
                folder.mkdir(parents=True)
                (folder / "mod.info").write_text(f"id={mod_id}\n", encoding="utf-8")

            beyond_lua = beyond / "42" / "media" / "lua" / "shared" / "BeyondTen"
            beyond_lua.mkdir(parents=True)
            (beyond_lua / "SkillRecoveryJournal.lua").write_text(
                'local mods = getActivatedMods()\nreturn mods:contains("SkillRecoveryJournal")\n',
                encoding="utf-8",
            )
            legacy_beyond_lua = beyond / "media" / "lua" / "shared" / "BeyondTen"
            legacy_beyond_lua.mkdir(parents=True)
            (legacy_beyond_lua / "SkillRecoveryJournal.lua").write_text(
                'local mods = getActivatedMods()\nreturn mods:contains("SkillRecoveryJournal")\n',
                encoding="utf-8",
            )
            music_lua = music / "42" / "media" / "lua" / "shared" / "core"
            music_lua.mkdir(parents=True)
            (music_lua / "NMTranslations.lua").write_text(
                'local MOD_ID = "NewMusic"\nreturn getModFileReader(MOD_ID, "x", false)\n',
                encoding="utf-8",
            )
            output = root / "output"

            build_modpack(
                BuildConfig(
                    name="Pack",
                    namespace="SSP",
                    sources=(root / "source",),
                    output=output,
                )
            )

            packed_beyond = (
                output
                / "Contents"
                / "mods"
                / "SSP_BeyondTen"
                / "42"
                / "media"
                / "lua"
                / "shared"
                / "BeyondTen"
                / "SkillRecoveryJournal.lua"
            ).read_text(encoding="utf-8")
            packed_legacy_beyond = (
                output
                / "Contents"
                / "mods"
                / "SSP_BeyondTen"
                / "media"
                / "lua"
                / "shared"
                / "BeyondTen"
                / "SkillRecoveryJournal.lua"
            ).read_text(encoding="utf-8")
            packed_music = (
                output
                / "Contents"
                / "mods"
                / "SSP_Talis New Music"
                / "42"
                / "media"
                / "lua"
                / "shared"
                / "core"
                / "NMTranslations.lua"
            ).read_text(encoding="utf-8")
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))

            self.assertIn('mods:contains("SSP_SkillRecoveryJournal")', packed_beyond)
            self.assertIn(
                'mods:contains("SSP_SkillRecoveryJournal")',
                packed_legacy_beyond,
            )
            self.assertIn('local MOD_ID = "SSP_NewMusic"', packed_music)
            self.assertEqual(len(manifest["compatibility_patches"]), 3)
            self.assertEqual(
                {patch["strategy"] for patch in manifest["compatibility_patches"]},
                {"activated_mod_lookup", "known_file_context"},
            )

    def test_known_compatibility_patch_fails_closed_after_upstream_change(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            music = root / "source" / "mods" / "Talis New Music"
            target = music / "42" / "media" / "lua" / "shared" / "core"
            target.mkdir(parents=True)
            (music / "mod.info").write_text("id=NewMusic\n", encoding="utf-8")
            (target / "NMTranslations.lua").write_text(
                'local CHANGED_MOD_ID = "NewMusic"\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(BuildError, "expected 1 match"):
                build_modpack(
                    BuildConfig(
                        name="Pack",
                        namespace="SSP",
                        sources=(root / "source",),
                        output=root / "output",
                    )
                )


if __name__ == "__main__":
    unittest.main()
