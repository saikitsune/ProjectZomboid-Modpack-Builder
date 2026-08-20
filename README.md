# PZ Modpack Builder

PZ Modpack Builder creates controlled, namespaced Project Zomboid Workshop packs. It downloads Workshop items through SteamCMD, freezes immutable source snapshots, rewrites Mod IDs and dependency metadata, validates the bundle, and generates files for Project Zomboid and AMP.

## Why this exists

A Workshop collection does not pin versions. Every included item continues updating independently, which can leave clients newer than a running server. This builder creates a single controlled Workshop package that changes only when its owner publishes a new build.

Bundled Mod IDs are namespaced so the pack can coexist with original Workshop items installed for other servers:

```text
CraftableMilitaryFences -> SaiPack_CraftableMilitaryFences
```

## Features

- Desktop GUI and reusable CLI/backend
- Non-blocking background builds with per-stage progress and responsive controls
- Official SteamCMD bootstrap installer for Windows and Linux
- Anonymous or authenticated SteamCMD login
- Optional Steam Guard code
- Credentials passed through a permission-restricted temporary SteamCMD runscript, never process arguments
- Temporary runscripts deleted immediately after SteamCMD exits or times out
- Password and Guard-code redaction from captured output
- Secret fields cleared after each GUI operation
- Persistent SteamCMD account selection using SteamCMD's own cached login token
- Saved sessions retain only the username and SteamCMD/library paths, never passwords or Guard codes
- Workshop URL and raw-ID parsing
- Batch Project Zomboid Workshop downloads using app ID `108600`
- Live Workshop download progress with SteamCMD activity, per-item percentages, and snapshot status
- SteamCMD Workshop creation and updates from generated packs
- Automatic Workshop description attribution with builder and bundled-mod links
- Cached account login support for uploads without reentering a password
- Safe upload VDF generation and automatic capture of newly published Workshop IDs
- Content-addressed immutable snapshots
- Workshop snapshot revision grouping with one active revision per Workshop item
- Latest-revision defaults with explicit older-snapshot rollback selection
- Snapshot capture, Workshop update, and Workshop manifest provenance
- Multiple `mod.info` variants, including root, `42`, and `42.20`
- Stable Mod ID namespacing
- Rewriting of `id`, `require`, `loadModAfter`, `loadModBefore`, and `incompatible`
- Targeted Lua activated-mod checks rewritten for direct calls, escaped IDs, and local aliases of `getActivatedMods()`
- Fail-closed compatibility patches for known mod-file, script, and runtime-ID contexts
- Exact Build 42 layout fixes for reviewed legacy folders and server-only Lua modules
- Build 42 preflight rejection of unrecognized root-only active mods
- Validated dependency relationships, applied compatibility patches, and occurrence-level categorized unresolved references recorded in `manifest.json`
- Automatic original-ID incompatibility guards
- Dependency ordering and cycle detection
- Duplicate Mod ID detection
- Interactive bundled-mod selection with metadata conflict enforcement
- Explicit bundled incompatibility blocking after selection
- Required-mod provider discovery from the active `mod.info`
- Recursive auto-selection and locking of available required bundled mods
- Missing or excluded required-mod blocking with actionable source guidance
- Hard-coded original Mod ID scans in Lua and other text files
- SHA-256 source manifests
- Automatic semantic modpack versions with patch, minor, and major rebuild options
- Complete prior-build archives in a marked sibling version-history directory
- Manifest and source-hash comparison for added, removed, updated, and reselected mods
- Generated Workshop change notes preloaded into the upload workflow
- Staged output replacement that preserves the current pack when a rebuild fails
- Saveable GUI project files that exclude Steam credentials
- AMP `WorkshopItems` and ordered `Mods` output

## Install

Python 3.11 or newer and [uv](https://docs.astral.sh/uv/) are required.

```bash
uv sync
```

Launch the GUI:

```bash
uv run pzmodpack-gui
```

Or use `run-gui.bat` on Windows and `run-gui.sh` on Linux.

### Linux desktop dependencies

PySide6 needs the normal Qt/OpenGL runtime libraries. Package names vary by distribution. On Debian-based systems they commonly include `libegl1`, `libgl1`, `libxkbcommon0`, and `libfontconfig1`.

Valve's Linux SteamCMD bootstrap starts a 32-bit binary. A 64-bit Linux installation must have its distribution's 32-bit runtime enabled. Windows SteamCMD does not have this Linux multilib requirement.

## GUI workflow

1. Open **Workshop downloads**.
2. Install SteamCMD or select an existing executable.
3. Choose anonymous login or enter a Steam account and optional Guard code.
4. Paste Workshop URLs or IDs, one per line.
5. Click **Download, snapshot, and add to pack**.
6. Open **Build pack** and scan the resulting sources. Historical snapshots are grouped by Workshop item instead of scanned as duplicate mods.
7. Click **Select snapshots and bundled mods...**. Choose one snapshot revision per Workshop item, then choose the bundled folders from those revisions.
8. Enter a stable namespace, select the active B41/B42 IDs, choose a preview, set visibility, and choose the version bump for rebuilds.
9. Choose optional bundled variants. The build opens snapshot selection automatically when an old project contains multiple revisions without a saved choice, and opens bundled selection when folders conflict or have an unsatisfied requirement.
10. Resolve every highlighted conflict or missing dependency and build the pack.
11. Review `manifest.json`, `change-notes.txt`, `amp-config.txt`, and hard-coded-ID warnings.
12. Switch to an authenticated Steam account and test the login.
13. Open **Workshop upload**, review or edit the generated change note, confirm redistribution permission, and upload.

Snapshot choices operate on Workshop items: every immutable revision of one Workshop ID appears in a single row and exactly one can be selected. The latest available revision is the default and is clearly labeled; older revisions remain available for rollback. The selector shows the full snapshot source in its tooltip, short content hash, true Workshop update date when Steam recorded one, local capture date, and latest/pinned status. Legacy snapshots created before provenance tracking show an unknown Workshop update date rather than a guessed value.

Bundled choices then operate on mod folders from only the chosen snapshots. Patch-level directories such as `42.19` and `42.20` inside `WaterPipes` remain together, while a separate folder such as `WaterPipesRemoved` can be excluded. The dialog shows the name, active Mod ID, all declared IDs, requirements, Workshop source, snapshot provenance, description, and incompatibilities for each folder. Selecting a mod recursively checks required providers already present in the selected revisions, and a provider cannot be unchecked while a selected mod depends on it. A missing provider is labeled `[MISSING]` with the exact required Mod ID so its Workshop item or source can be added. Confirmation and builds remain blocked until requirements and conflicts are resolved.

Workshop ID `0` creates a new item. After a successful creation, SteamCMD updates the VDF and the builder writes the new ID into `manifest.json`, `workshop.txt`, `amp-config.txt`, and the GUI field. An existing Workshop ID updates that item instead.

At upload time, the builder appends a Steam BBCode footer to the user-entered description. It links this project with the uploading program version and lists every included mod folder that has a recorded source Workshop ID. The footer is regenerated from `manifest.json` for each upload, so updating the same Workshop item does not duplicate it. Local sources without a Workshop ID are omitted. Because SteamCMD's Workshop VDF parser treats embedded straight double quotes as delimiters, text values automatically use visually equivalent typographic quotes; paths containing double quotes are rejected instead of silently redirected. Upload preflight measures the normalized UTF-8 text and reports an actionable error instead of silently dropping attribution when the generated description would exceed Steam's 8,000-byte limit.

The program never replaces or deletes a locked snapshot when an upstream item changes. Downloading again creates or reuses a snapshot based on the content hash. Builds follow the latest downloaded revision by default while keeping every older revision selectable. If an older revision was explicitly selected, new downloads preserve that pin until it is changed in the snapshot selector.

After SteamCMD downloads an item, the builder reads Steam's local `appworkshop_108600.acf` manifest and records the Workshop update time and manifest ID in `snapshot.json` alongside the local UTC capture time. Missing or malformed Steam metadata never blocks a download. Existing format-1 snapshots are upgraded safely when the same content is downloaded again; only their metadata changes, never the frozen mod payload.

### Modpack version history

The first successful build at an output path is version `1.0.0`. Rebuilding the same generated output advances the version using the selected patch, minor, or major bump. The configured output path always remains the latest upload-ready build, so saved projects and upload settings do not need to change between releases.

Before the new version replaces the latest build, it is completed in a marked staging directory. The prior complete output is then moved into a marked sibling history directory such as `sai-pack.versions/v1.0.0`. A failed build leaves the last successful output untouched.

Every versioned manifest records the pack version, previous version, builder version, UTC build time, active source Mod IDs, structured detected changes, and the generated Workshop change note. Comparisons use Workshop ID plus source folder identity and SHA-256 source hashes to detect added, removed, and updated bundled mods. Active-version and pack-setting changes are recorded separately. The generated note is written to `change-notes.txt` and loaded into the GUI upload tab for review.

During a batch download, the Workshop tab shows the current item, SteamCMD's item percentage when available, completed-item count, and immutable-snapshot progress. The overall bar reserves its final stage for hashing and snapshotting the downloaded files.

## Steam login security

Public Project Zomboid Workshop items usually work with anonymous SteamCMD login.

For account login:

- The GUI masks the password.
- Password and Steam Guard values are written to a permission-restricted temporary SteamCMD runscript.
- The runscript path, not the credentials, is passed to SteamCMD as a process argument.
- The temporary runscript is deleted immediately after SteamCMD exits or times out.
- Captured output is redacted.
- Secret fields are cleared after use.
- After a verified login, the application saves only the username and SteamCMD/library paths.
- SteamCMD owns and reuses its cached machine authorization; the application does not copy or store that token.
- The saved account is restored when the GUI opens again and can be cleared with **Forget saved account**.
- Project files, manifests, snapshots, and session preferences never include the password or Guard code.

The GUI requires SteamCMD to confirm a complete login before saving the account. It accepts both SteamCMD output formats: `Logged in OK`, or the Windows sequence `Logging in user ... to Steam Public...OK` followed by `Waiting for user info...OK`. Login checks stop after 90 seconds instead of waiting indefinitely. A timeout message explains whether SteamCMD may be completing its first-run update or waiting for a Steam Guard code.

SteamCMD may require a fresh Guard code when the machine has not previously been authorized. Private or restricted Workshop items may require an account with access. The **Forget saved account** button removes the application's saved account selection; SteamCMD manages its own credential cache separately.

## CLI

Inspect sources:

```bash
uv run pzmodpack scan /path/to/item /path/to/another-item --json
```

When multiple revisions of one Workshop item are supplied, scan and build use the latest by default. Pin an older revision for a reproducible CLI operation with `--snapshot-revision WORKSHOP_ID=FULL_SHA256`.

Install SteamCMD:

```bash
uv run pzmodpack steam-install --destination ./tools/steamcmd
```

Test anonymous login:

```bash
uv run pzmodpack steam-login \
  --steamcmd ./tools/steamcmd/steamcmd.sh \
  --library ./steam-library \
  --anonymous
```

Authenticated CLI login prompts for the password and Guard code without accepting them as command-line options:

```bash
uv run pzmodpack steam-login \
  --steamcmd ./tools/steamcmd/steamcmd.exe \
  --library ./steam-library \
  --username your-steam-name
```

Download and snapshot Workshop items:

```bash
uv run pzmodpack steam-download \
  2921417999 \
  'https://steamcommunity.com/sharedfiles/filedetails/?id=3778832646' \
  --steamcmd ./tools/steamcmd/steamcmd.exe \
  --library ./steam-library \
  --snapshots ./snapshots \
  --anonymous
```

Build a pack:

```bash
uv run pzmodpack build \
  --name "Sai's Project Zomboid Pack" \
  --namespace SaiPack \
  --source ./snapshots/2921417999/0123456789abcdef \
  --source ./snapshots/3778832646/fedcba9876543210 \
  --output ./build/sai-pack \
  --workshop-id 1234567890 \
  --preview ./preview.png \
  --visibility 2 \
  --version-bump patch \
  --active-id Furry=FurryModB42 \
  --include-mod-id FurryModB42
```

An active-ID override selects the version-specific ID written to AMP. Dependency references to alternate IDs from the same source folder are also redirected to the selected packed ID, so Build 42 dependents do not accidentally require the bundled Build 41 alias.

Repeat `--include-mod-id` to select multiple bundled folders. A folder is included when it declares any selected ID; all of that folder's game-version directories are copied together. Omitting the option preserves the default of including every discovered folder, and validation still blocks incompatible selections.

Upload using a cached SteamCMD account session:

```bash
uv run pzmodpack steam-upload \
  --steamcmd ./tools/steamcmd/steamcmd.exe \
  --library ./steam-library \
  --output ./build/sai-pack \
  --username your-steam-name \
  --cached-login \
  --confirm-permissions
```

The upload command uses the change note generated by the latest build. Pass `--change-note "..."` to override it. Omit `--cached-login` to receive hidden password and Steam Guard prompts. Uploads are refused unless `--confirm-permissions` is supplied.

## Generated output

```text
build/sai-pack/
├── .pzmodpack-output
├── Contents/
│   └── mods/
│       ├── SaiPack_FirstMod/
│       └── SaiPack_SecondMod/
├── amp-config.txt
├── change-notes.txt
├── manifest.json
├── preview.png
├── workshop.txt
└── workshop_upload.vdf       # created when uploading

build/sai-pack.versions/
├── .pzmodpack-history
└── v1.0.0/                    # complete prior upload-ready build
```

`workshop.txt` defaults to visibility `2`, which is private. Publish staging builds privately and change visibility only when ready.

## Validation behavior

Build-blocking errors:

- Duplicate Mod IDs
- Dependency cycles
- Explicit incompatibilities between bundled mods
- Invalid namespace
- No discovered mods
- An active root-only legacy mod in a pack whose selected IDs require Build 42
- Attempting to overwrite an output or version-history directory not created by this builder

Warnings are categorized in `manifest.json`:

- `metadata`: unresolved or external metadata dependency
- `runtime_mod_lookup`: an original ID remains in an activated-mod lookup, mod-info lookup, or comparison against a runtime `getId()`/`getModID()` value after automatic rewrites
- `mod_file_access`: an original ID remains in a `getModFileReader` or `getModFileWriter` context
- `content_namespace`: the token is used as a Lua table, function namespace, or options identifier rather than a runtime Mod-ID lookup
- `ambiguous_string`: the builder cannot safely determine the token's purpose

A remaining `runtime_mod_lookup` or `mod_file_access` warning means the cited occurrence was **not** automatically fixed and needs review or a deterministic compatibility patch. Classification is per occurrence, so a harmless content namespace elsewhere in the same file no longer masks a sensitive lookup. Internal identifiers such as `SeedSeasonIndicator` used for Lua tables or `PZAPI.ModOptions` are classified as `content_namespace`, retained in `manifest.json`, and omitted from the primary actionable GUI warning list.

The builder deliberately avoids blind global replacement. A Mod ID can also be a module filename, translation key, save key, script namespace, option identifier, or comment. Targeted API rewrites and fail-closed compatibility patches are used instead.

Known layout and script fixes are equally strict: they run only for an exact source folder, path, and expected content. They are recorded in `compatibility_patches` with strategies such as `known_layout_context`, `known_file_relocation`, `known_file_passthrough`, and `known_file_context`. A passthrough records a reviewed upstream layout that no longer needs the old builder edit. If an upstream mod changes every reviewed layout or expected text, the build stops for review instead of applying a speculative edit.

## Project files

GUI projects use the suffix `.pzpack.json` and store paths, Workshop IDs, pack metadata, source and bundled-mod selections, selected snapshot revisions, preview/visibility, version-bump preference, and active Mod ID overrides. Existing project files without snapshot choices continue loading and default each Workshop item to its latest available revision. They do not store Steam usernames, passwords, or Steam Guard codes. `manifest.json` records the selected and available snapshot revisions with their hashes and dates, plus included Mod IDs and excluded source folders, so every build's source choice and release diff can be audited.

## Redistribution and permissions

A bundled pack republishes other authors' files and may modify metadata. Confirm each mod's license or obtain permission before publishing. Preserve attribution and source links in the Workshop description and manifest.

## Tests

```bash
uv run python -m unittest discover -s tests -v
```

Headless Linux GUI tests may require:

```bash
QT_QPA_PLATFORM=offscreen uv run python -m unittest discover -s tests -v
```

## Current limitations

- SteamCMD output is collected per operation instead of streamed line by line.
- Targeted Lua checks using `getActivatedMods():contains("OriginalId")` are safely rewritten to the packed ID; remaining ambiguous hard-coded references are warnings.
- SteamCMD uploads require a preview image and a Steam account that owns or can contribute to the destination Workshop item.
- The GUI suggests the deepest/latest discovered `mod.info` ID for versioned folders; review or override it before publishing.
- A real SteamCMD download or upload requires a supported host runtime. Linux needs Valve's 32-bit runtime dependencies.
