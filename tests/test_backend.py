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
            (framework / "common").mkdir(parents=True)
            (dependent / "42" / "media" / "lua" / "shared").mkdir(parents=True)
            (framework / "common" / "mod.info").write_text(
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
                output
                / "Contents"
                / "mods"
                / "SaiPack_Framework"
                / "common"
                / "mod.info"
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

    def test_audit_classifies_each_sensitive_occurrence_independently(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            mod = root / "source" / "mods" / "AuditExample"
            lua = mod / "media" / "lua" / "client"
            lua.mkdir(parents=True)
            (mod / "mod.info").write_text("id=AuditExample\n", encoding="utf-8")
            (lua / "audit.lua").write_text(
                "local AuditExample = {}\n"
                'PZAPI.ModOptions:create("AuditExample", "Audit Example")\n'
                "local modID = modInfo:getId()\n"
                'if modID ~= "AuditExample" then return end\n'
                'getModFileWriter("AuditExample", "settings.json", true, false)\n',
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
                if detail.get("file", "").endswith("audit.lua")
                and "AuditExample" in detail.get("original_ids", [])
            ]
            self.assertEqual(
                {detail["category"] for detail in details},
                {"content_namespace", "runtime_mod_lookup", "mod_file_access"},
            )
            self.assertTrue(all(detail.get("line_numbers") for detail in details))

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
            (dependent / "common").mkdir(parents=True)
            (dependent / "common" / "mod.info").write_text(
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
                output
                / "Contents"
                / "mods"
                / "Pack_Dependent"
                / "common"
                / "mod.info"
            ).read_text(encoding="utf-8")
            self.assertIn("require=Pack_ExampleB42", dependent_info)

    def test_applies_runtime_lookup_patches_and_records_them(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            mods = root / "source" / "mods"
            journal = mods / "Skill Recovery Journal"
            beyond = mods / "BeyondTen"
            music = mods / "Talis New Music"
            simple_status = mods / "SimpleStatus"
            error_magnifier = mods / "errorMagnifier"
            for folder, mod_id in (
                (journal, "SkillRecoveryJournal"),
                (beyond, "BeyondTen"),
                (music, "NewMusic"),
                (simple_status, "simpleStatus"),
                (error_magnifier, "errorMagnifier"),
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
            music_catalog = music / "42" / "media" / "lua" / "shared" / "loot"
            music_catalog.mkdir(parents=True)
            (music_catalog / "NMManagedSpawnCatalog.lua").write_text(
                'local function isTargetModId(modId)\n'
                '    local lower = string.lower(modId)\n'
                '    return lower == "newmusic" or lower == "talisnewmusic"\n'
                'end\n'
                'if okValue and valueContainsRequiredMod(value, "NewMusic") then\n'
                '    compatible[modId] = true\n'
                'end\n',
                encoding="utf-8",
            )
            (music_catalog / "NMLootResolvedPools.lua").write_text(
                'require "loot/NMManagedSpawnCatalog"\n'
                'require "loot/NMLootPolicySnapshot"\n'
                'require "loot/NMLootRealizationAuthority"\n'
                "NMLootResolvedPools = NMLootResolvedPools or {}\n",
                encoding="utf-8",
            )
            simple_lua = simple_status / "media" / "lua" / "client"
            simple_lua.mkdir(parents=True)
            (simple_lua / "ss.main.lua").write_text(
                'local cfg = utils.fn.loadConfig("simpleStatus", playerNum)\n'
                'bar = SSBar:new(playerObj, cfg, "simpleStatus")\n',
                encoding="utf-8",
            )
            (simple_lua / "ss.utils.lua").write_text(
                'local file, _ = getModFileWriter("simpleStatus", "log.txt", true, true)\n',
                encoding="utf-8",
            )
            error_lua = error_magnifier / "media" / "lua" / "client"
            error_lua.mkdir(parents=True)
            (error_lua / "fingerPrint_Main.lua").write_text(
                'local modID = modInfo and modInfo:getId()\n'
                'if modID and modID~="errorMagnifier" then\n'
                '    print(modID)\n'
                'end\n',
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
            packed_music_catalog = (
                output
                / "Contents"
                / "mods"
                / "SSP_Talis New Music"
                / "42"
                / "media"
                / "lua"
                / "shared"
                / "loot"
                / "NMManagedSpawnCatalog.lua"
            ).read_text(encoding="utf-8")
            relocated_resolved_pools = (
                output
                / "Contents"
                / "mods"
                / "SSP_Talis New Music"
                / "42"
                / "media"
                / "lua"
                / "server"
                / "loot"
                / "NMLootResolvedPools.lua"
            )
            packed_simple_main = (
                output
                / "Contents"
                / "mods"
                / "SSP_SimpleStatus"
                / "media"
                / "lua"
                / "client"
                / "ss.main.lua"
            ).read_text(encoding="utf-8")
            packed_simple_utils = (
                output
                / "Contents"
                / "mods"
                / "SSP_SimpleStatus"
                / "media"
                / "lua"
                / "client"
                / "ss.utils.lua"
            ).read_text(encoding="utf-8")
            packed_fingerprint = (
                output
                / "Contents"
                / "mods"
                / "SSP_errorMagnifier"
                / "media"
                / "lua"
                / "client"
                / "fingerPrint_Main.lua"
            ).read_text(encoding="utf-8")
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))

            self.assertIn('mods:contains("SSP_SkillRecoveryJournal")', packed_beyond)
            self.assertIn(
                'mods:contains("SSP_SkillRecoveryJournal")',
                packed_legacy_beyond,
            )
            self.assertIn('local MOD_ID = "SSP_NewMusic"', packed_music)
            self.assertIn('lower == "ssp_newmusic"', packed_music_catalog)
            self.assertIn(
                'valueContainsRequiredMod(value, "SSP_NewMusic")',
                packed_music_catalog,
            )
            self.assertTrue(relocated_resolved_pools.is_file())
            self.assertFalse(
                (
                    output
                    / "Contents"
                    / "mods"
                    / "SSP_Talis New Music"
                    / "42"
                    / "media"
                    / "lua"
                    / "shared"
                    / "loot"
                    / "NMLootResolvedPools.lua"
                ).exists()
            )
            self.assertIn('loadConfig("SSP_simpleStatus", playerNum)', packed_simple_main)
            self.assertIn('SSBar:new(playerObj, cfg, "SSP_simpleStatus")', packed_simple_main)
            self.assertIn(
                'getModFileWriter("SSP_simpleStatus", "log.txt"',
                packed_simple_utils,
            )
            self.assertIn('modID~="SSP_errorMagnifier"', packed_fingerprint)
            self.assertEqual(len(manifest["compatibility_patches"]), 10)
            self.assertEqual(
                {patch["strategy"] for patch in manifest["compatibility_patches"]},
                {
                    "activated_mod_lookup",
                    "known_file_context",
                    "known_file_relocation",
                },
            )
            self.assertIn(
                "new-music-server-loot-module",
                {
                    patch.get("name")
                    for patch in manifest["compatibility_patches"]
                },
            )
            self.assertEqual(
                {
                    patch["name"]
                    for patch in manifest["compatibility_patches"]
                    if patch["strategy"] == "known_file_context"
                },
                {
                    "new-music-mod-file-reader-id",
                    "new-music-target-mod-id",
                    "new-music-child-requirement-id",
                    "simple-status-load-config-id",
                    "simple-status-bar-config-id",
                    "simple-status-log-writer-id",
                    "error-magnifier-self-id",
                },
            )

    def test_promotes_known_legacy_mod_to_build_42_layout(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            mods = root / "source" / "mods"
            furry = mods / "Furry" / "42"
            deceased = mods / "Anthro_Deceased"
            furry.mkdir(parents=True)
            (deceased / "media").mkdir(parents=True)
            (furry / "mod.info").write_text(
                "id=FurryModB42\n",
                encoding="utf-8",
            )
            (deceased / "mod.info").write_text(
                "name=Deceased Anthro Survivors\n"
                "id=DeceasedAnthros\n"
                "poster=poster.png\n"
                "require=FurryModB42\n",
                encoding="utf-8",
            )
            (deceased / "Poster.png").write_bytes(b"poster")
            (deceased / "media" / "asset.txt").write_text(
                "asset\n",
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

            packed = output / "Contents" / "mods" / "SSP_Anthro_Deceased"
            packed_info = (packed / "42" / "mod.info").read_text(encoding="utf-8")
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertFalse((packed / "mod.info").exists())
            self.assertFalse((packed / "media").exists())
            self.assertTrue((packed / "42" / "media" / "asset.txt").is_file())
            self.assertTrue((packed / "42" / "poster.png").is_file())
            packed_entry_names = {
                path.name for path in (packed / "42").iterdir()
            }
            self.assertIn("poster.png", packed_entry_names)
            self.assertNotIn("Poster.png", packed_entry_names)
            self.assertIn("id=SSP_DeceasedAnthros", packed_info)
            self.assertIn("require=SSP_FurryModB42", packed_info)
            self.assertIn(
                "deceased-anthros-build-42-layout",
                {
                    patch.get("name")
                    for patch in manifest["compatibility_patches"]
                },
            )
            self.assertIn(
                "Mods=SSP_FurryModB42;SSP_DeceasedAnthros;",
                (output / "amp-config.txt").read_text(encoding="utf-8"),
            )

    def test_removes_known_shelter_hold_base_self_imports(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            shelter = root / "source" / "mods" / "ShelterHold_Beehive"
            script_prefix = (
                "module Base\n"
                "{\n"
                "    imports\n"
                "    {\n"
                "        Base\n"
                "    }\n\n"
                "    evolvedrecipe HoneyCake\n"
                "    {\n"
                "    }\n"
                "}\n"
            )
            for version in ("42", "42.13"):
                script = shelter / version / "media" / "scripts" / "generated"
                script.mkdir(parents=True)
                (shelter / version / "mod.info").write_text(
                    "id=ShelterHold_Beehive\n",
                    encoding="utf-8",
                )
                (script / "Hold_evolvedrecipes.txt").write_text(
                    script_prefix,
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

            packed = output / "Contents" / "mods" / "SSP_ShelterHold_Beehive"
            for version in ("42", "42.13"):
                rewritten = (
                    packed
                    / version
                    / "media"
                    / "scripts"
                    / "generated"
                    / "Hold_evolvedrecipes.txt"
                ).read_text(encoding="utf-8")
                self.assertIn("module Base", rewritten)
                self.assertNotIn("imports", rewritten)
                self.assertIn("evolvedrecipe HoneyCake", rewritten)
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                {
                    patch.get("name")
                    for patch in manifest["compatibility_patches"]
                },
                {
                    "shelter-hold-base-self-import-42",
                    "shelter-hold-base-self-import-42-13",
                },
            )

    def test_build_42_pack_rejects_unknown_root_only_active_mod(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            versioned = root / "source" / "mods" / "Versioned" / "42"
            legacy = root / "source" / "mods" / "Legacy"
            versioned.mkdir(parents=True)
            legacy.mkdir(parents=True)
            (versioned / "mod.info").write_text(
                "id=VersionedB42\n",
                encoding="utf-8",
            )
            (legacy / "mod.info").write_text(
                "id=LegacyOnly\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(BuildError, "legacy root mod.info"):
                build_modpack(
                    BuildConfig(
                        name="Pack",
                        namespace="SSP",
                        sources=(root / "source",),
                        output=root / "output",
                    )
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
            resolved = music / "42" / "media" / "lua" / "shared" / "loot"
            resolved.mkdir(parents=True)
            (resolved / "NMLootResolvedPools.lua").write_text(
                'require "loot/NMManagedSpawnCatalog"\n'
                'require "loot/NMLootPolicySnapshot"\n'
                'require "loot/NMLootRealizationAuthority"\n',
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
