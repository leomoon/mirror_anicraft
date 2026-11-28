#!/usr/bin/env python3
"""Synthetic tests for the Linux Nightly launchers; no app build, no window.

The plain nightly tar's `anicraft` is the canonical shared template
(craft-common-actions scripts/nightly-launcher.sh.in), which the shared nightly producer accepts
byte for byte; `Template` pins its exact argv. The distro formats install their own launcher
(packaging/linux/nightly-package-launch.sh): the deb/rpm, the AppImage's AppRun, the Arch handoff and
the Flatpak bundle, each exercised here exactly as package.sh, flatpak-bundle.sh and
nightly-arch-tar.sh install it, with a recorder standing in for the real program.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
LINUX = HERE / "linux"
LAUNCHER = LINUX / "nightly-launch.sh"
PACKAGE_LAUNCHER = LINUX / "nightly-package-launch.sh"
# The tar launcher is the canonical template, so its profile folder is the nightly package identity.
IDENTITY = "ai.storyteller.anicraft.nightly"
RECORDER = '#!/bin/sh\nfor arg in "$@"; do printf \'%s\\n\' "$arg"; done\n'


class Layout(unittest.TestCase):
    """Each format's install layout, run through the shared launcher."""

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name).resolve()
        self.home = self.root / "home with space"
        self.home.mkdir()

    # -- building the five layouts ------------------------------------------------------------
    def script(self, path, text=RECORDER):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o755)
        return path

    def launcher(self, path, source=PACKAGE_LAUNCHER):
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, path)
        path.chmod(0o755)
        return path

    def layouts(self):
        """The distro bundle layouts, each run in place. The deb/rpm is not among them: a test host
        cannot install into /usr/bin, so that one arm is pinned by contract and by failing closed
        (below). The plain nightly tar is the canonical template and is covered by `Template`."""
        made = {}
        base = self.root / "layouts"
        # AppImage: AppRun at the AppDir root runs usr/bin/anicraft
        appdir = base / "AniCraft.AppDir"
        made["appimage"] = (self.launcher(appdir / "AppRun"), self.script(appdir / "usr/bin/anicraft"))
        # Arch handoff: bin/anicraft runs bin/.anicraft-nightly/anicraft
        arch = base / "arch"
        made["arch"] = (self.launcher(arch / "bin/anicraft"), self.script(arch / "bin/.anicraft-nightly/anicraft"))
        # Flatpak: /app/bin/anicraft-nightly runs /app/bin/anicraft-bin
        flatpak = base / "app"
        made["flatpak"] = (self.launcher(flatpak / "bin/anicraft-nightly"), self.script(flatpak / "bin/anicraft-bin"))
        return made

    def run_launcher(self, command, *arguments, env=None, cwd=None):
        environment = {"PATH": "/usr/bin:/bin", "HOME": str(self.home)}
        environment.update(env or {})
        return subprocess.run(
            [str(command), *arguments], capture_output=True, text=True, timeout=30,
            env=None if environment is None else {k: v for k, v in environment.items() if v is not None},
            cwd=str(cwd or self.root),
        )

    def profile(self, data_home=None):
        return str(Path(data_home or self.home / ".local/share") / "anicraft-nightly/profile")

    def protocol(self, *files, data_home=None):
        return ["--launch-profile", "nightly-v1", "--profile-root", self.profile(data_home), "--", *files]

    def arguments(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()

    # -- the contract every format shares -----------------------------------------------------
    def test_every_format_starts_the_real_program_in_the_nightlys_own_profile(self):
        files = ["scene with spaces.anicraft", "--other-name.anicraft"]
        for name, (command, _) in self.layouts().items():
            with self.subTest(layout=name):
                self.assertEqual(self.arguments(self.run_launcher(command, *files)), self.protocol(*files))

    def test_the_system_install_is_one_absolute_path_behind_the_bin_check(self):
        # A deb/rpm install is the only layout with the program in a lib folder, and there it is one
        # fixed absolute path, reached only when the launcher really is in /usr/bin. No candidate is
        # ever computed from `..`, so no bundle can plant one beside itself.
        launcher = PACKAGE_LAUNCHER.read_text()
        self.assertIn('[ "$here" = /usr/bin ]', launcher)
        self.assertIn('/usr/lib/anicraft-nightly/anicraft ', launcher)
        self.assertNotIn("..", launcher)

    def test_outside_usr_bin_a_co_installed_stable_program_and_a_planted_lib_are_both_refused(self):
        # The deb/rpm launcher run from anywhere but /usr/bin: a stable `anicraft` beside it and a
        # program planted in a sibling lib folder are not ours, so it refuses instead of running one.
        prefix = self.root / "prefix"
        command = self.launcher(prefix / "bin/anicraft-nightly")
        self.script(prefix / "bin/anicraft", "#!/bin/sh\necho THE-STABLE-PROGRAM\n")
        self.script(prefix / "lib/anicraft-nightly/anicraft", "#!/bin/sh\necho PLANTED-BY-ANOTHER-USER\n")
        result = self.run_launcher(command, "walk.anicraft")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not where this launcher was installed", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_a_planted_program_beside_a_bundle_is_never_run(self):
        # An AppImage mount and an extracted tar sit under a folder another local user may own, so a
        # `../lib/anicraft-nightly/anicraft` there must never stand in for the bundle's own program.
        layouts = self.layouts()
        for name in ("appimage", "arch", "flatpak"):
            command, _ = layouts[name]
            for planted in (command.parent.parent / "lib/anicraft-nightly/anicraft",
                            command.parent.parent / "lib/anicraft-nightly/anicraft-bin"):
                self.script(planted, "#!/bin/sh\necho PLANTED-BY-ANOTHER-USER\n")
            with self.subTest(layout=name):
                result = self.run_launcher(command, "walk.anicraft")
                self.assertEqual(self.arguments(result), self.protocol("walk.anicraft"))
                self.assertNotIn("PLANTED", result.stdout)

    def test_a_launcher_command_line_is_passed_through_unwrapped(self):
        # The Launcher starts a package this way already: wrapping it again would hide its root.
        supplied = self.protocol("scene.anicraft", data_home=self.root / "launcher-data")
        for name, (command, _) in self.layouts().items():
            with self.subTest(layout=name):
                self.assertEqual(self.arguments(self.run_launcher(command, *supplied)), supplied)

    def test_a_supplied_delimiter_is_not_doubled(self):
        for name, (command, _) in self.layouts().items():
            with self.subTest(layout=name):
                self.assertEqual(self.arguments(self.run_launcher(command, "--", "scene.anicraft")), self.protocol("scene.anicraft"))

    def test_the_profile_follows_xdg_data_home(self):
        data_home = self.root / "xdg with space"
        for name, (command, _) in self.layouts().items():
            with self.subTest(layout=name):
                result = self.run_launcher(command, "walk.anicraft", env={"XDG_DATA_HOME": str(data_home)})
                self.assertEqual(self.arguments(result), self.protocol("walk.anicraft", data_home=data_home))

    def test_a_relative_xdg_data_home_is_ignored(self):
        for name, (command, _) in self.layouts().items():
            with self.subTest(layout=name):
                result = self.run_launcher(command, "walk.anicraft", env={"XDG_DATA_HOME": "relative/share"})
                self.assertEqual(self.arguments(result), self.protocol("walk.anicraft"))

    def test_a_version_query_answers_the_launchers_own_form(self):
        for name, (command, _) in self.layouts().items():
            for flag in ("--version", "-V"):
                with self.subTest(layout=name, flag=flag):
                    expected = ["--launch-profile", "nightly-v1", "--profile-root", self.profile(), flag]
                    self.assertEqual(self.arguments(self.run_launcher(command, flag)), expected)

    def test_a_file_named_like_a_flag_is_still_a_file(self):
        for name, (command, _) in self.layouts().items():
            with self.subTest(layout=name):
                files = ["--version", "--launch-profile", "nightly-v1"]
                self.assertEqual(self.arguments(self.run_launcher(command, *files)), self.protocol(*files))

    def test_no_arguments_is_a_bare_delimiter(self):
        for name, (command, _) in self.layouts().items():
            with self.subTest(layout=name):
                self.assertEqual(self.arguments(self.run_launcher(command)), self.protocol())

    def test_the_launcher_works_when_it_is_found_on_path(self):
        self.layouts()
        arch = self.root / "layouts/arch/bin"
        # The Arch packager installs the handoff's bin/anicraft as anicraft-nightly (or keeps it).
        (arch / "anicraft").rename(arch / "anicraft-nightly")
        result = subprocess.run(
            ["sh", "-c", 'exec anicraft-nightly "$1"', "sh", "walk.anicraft"],
            capture_output=True, text=True, timeout=30,
            env={"PATH": f"{arch}:/usr/bin:/bin", "HOME": str(self.home)},
        )
        self.assertEqual(self.arguments(result), self.protocol("walk.anicraft"))

    def test_a_missing_or_symlinked_program_is_refused_without_recursing(self):
        layouts = self.layouts()
        for name, (command, program) in layouts.items():
            with self.subTest(layout=name, case="missing"):
                moved = program.with_name(program.name + ".moved")
                program.rename(moved)
                try:
                    result = self.run_launcher(command, "walk.anicraft")
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("not where this launcher was installed", result.stderr)
                    self.assertEqual(result.stdout, "")
                finally:
                    moved.rename(program)
                with self.subTest(layout=name, case="symlink"):
                    program.unlink()
                    program.symlink_to(self.root / "elsewhere")
                    try:
                        result = self.run_launcher(command, "walk.anicraft")
                        self.assertEqual(result.returncode, 2)
                        self.assertIn("not where this launcher was installed", result.stderr)
                    finally:
                        program.unlink()
                        program.write_text(RECORDER)
                        program.chmod(0o755)

    def test_home_is_required_before_anything_runs(self):
        for name, (command, _) in self.layouts().items():
            with self.subTest(layout=name):
                result = self.run_launcher(command, "walk.anicraft", env={"HOME": None})
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn("HOME", result.stderr)

    def test_the_profile_never_sits_in_or_above_stables_folders(self):
        profile = Path(self.profile())
        parts = profile.parts
        self.assertEqual(parts[-2:], ("anicraft-nightly", "profile"))
        for stable in (".config", ".cache", ".local/state"):
            self.assertNotIn(str(self.home / stable / "anicraft"), str(profile))


class InstallContract(unittest.TestCase):
    """Every Linux format must install the one launcher, not its own copy of the protocol."""

    def read(self, name):
        return (LINUX / name).read_text()

    def test_every_format_installs_the_shared_launcher(self):
        package = self.read("package.sh")
        # deb/rpm GUI command and the AppImage AppRun
        self.assertIn('install -Dm755 "$HERE/nightly-package-launch.sh" "$NFPM_STAGE/usr/bin/$NFPM_BIN"', package)
        self.assertIn('install -Dm755 "$HERE/nightly-package-launch.sh" "$APPDIR/AppRun"', package)
        self.assertNotIn("<<'WRAPPER'", package)
        self.assertIn('install -Dm755 "$HERE/nightly-package-launch.sh" "$WORK/stage/bin/anicraft-nightly"', self.read("flatpak-bundle.sh"))
        self.assertIn('cp "$here/nightly-package-launch.sh" "$stage/bin/anicraft"', self.read("nightly-arch-tar.sh"))
        self.assertIn('install -m755 "$ROOT/packaging/linux/nightly-launch.sh" "$STAGE/anicraft"', self.read("nightly.sh"))
        self.assertFalse((LINUX / "nightly-arch-launch.sh").exists(), "the Arch launcher is the distro one file")

    def test_no_format_still_moves_the_settings_folder_through_the_environment(self):
        for name in ("flatpak-bundle.sh", "nightly-arch-tar.sh", "nightly.sh", "nightly-package-launch.sh"):
            with self.subTest(file=name):
                self.assertNotIn("XDG_CONFIG_HOME", self.read(name))
        # package.sh keeps it only for the headless CLI, which has no profile protocol (the GUI
        # command and AppRun are the launcher).
        package = self.read("package.sh")
        self.assertEqual(package.count("XDG_CONFIG_HOME"), 1)
        self.assertGreater(package.index("XDG_CONFIG_HOME"), package.index("<<'CLI_WRAPPER'"))

    def test_the_nightly_launcher_passes_the_flavors_protocol(self):
        launcher = PACKAGE_LAUNCHER.read_text()
        self.assertIn('exec "$real_bin" --launch-profile nightly-v1 --profile-root "$profile_root" -- "$@"', launcher)
        self.assertIn('"$real_bin" --launch-profile nightly-v1 --profile-root "$profile_root" "$1"', launcher)

    def test_the_formats_that_ship_a_desktop_entry_point_it_at_the_launcher(self):
        package = self.read("package.sh")
        self.assertIn("Exec=$NFPM_BIN\\1", package)
        self.assertRegex(package, r"StartupWMClass=ai\.storyteller\.anicraft\.nightly-v1")
        flatpak = self.read("flatpak-bundle.sh")
        self.assertIn("Exec=anicraft-nightly", flatpak)
        self.assertIn("s|^command: anicraft$|command: anicraft-nightly|", flatpak)
        arch = self.read("nightly-arch-tar.sh")
        self.assertIn("Exec=anicraft-nightly", arch)
        self.assertIn("StartupWMClass=$nightly_id", arch)

    def test_the_tar_is_the_standard_launcher_and_bin_layout(self):
        # `<app>` is the launcher and `<app>-bin` the GUI it starts, as in every app in the batch,
        # so an unpacked tar starts in its own profile and the shared smoke can query the entry.
        nightly = self.read("nightly.sh")
        self.assertIn('install -m755 "$NIGHTLY_GUI" "$STAGE/anicraft-bin"', nightly)
        self.assertIn('install -m755 "$ROOT/packaging/linux/nightly-launch.sh" "$STAGE/anicraft"', nightly)
        self.assertIn('[[ "$("$STAGE/anicraft" --version)" == "AniCraft $VERSION (nightly-v1)"', nightly)
        archive = (HERE / "nightly-archive.py").read_text()
        self.assertIn('"anicraft-bin"', archive)
        self.assertNotIn('"anicraft-nightly",\n', archive)

    def test_the_flatpak_runs_its_own_gui_and_never_asks_for_a_mime_database_it_drops(self):
        flatpak = self.read("flatpak-bundle.sh")
        # The sandbox's GUI is `anicraft-bin`, which is what the launcher looks for there.
        self.assertIn("/app/bin/anicraft-bin", flatpak)
        # Nightly deletes share/mime, so the default test must not list it (ls exits 2 under set -e).
        self.assertIn("SHARE_PATHS='/app/share/applications /app/share/metainfo /app/share/icons/hicolor/*/apps'", flatpak)
        self.assertIn("if [[ \"$FLATPAK_ID\" = \"$APP_ID\" ]]; then", flatpak)

    def test_nightly_metadata_declares_no_file_associations(self):
        for name in ("package.sh", "flatpak-bundle.sh", "nightly-arch-tar.sh"):
            with self.subTest(file=name):
                self.assertIn("/<mediatype>/d", self.read(name))

    def test_the_nightly_build_uses_the_apps_default_features(self):
        common = (HERE / "nightly-common.sh").read_text()
        # `-p anicraft` with its default features is the GUI a stable build ships (sound output and
        # the GPU stage); a --features/--no-default-features here would ship a different app.
        self.assertIn("cargo build --release --locked -p anicraft --target", common)
        self.assertNotIn("--no-default-features", common)
        self.assertNotIn("--features", common)

    def test_the_prepared_identity_reaches_build_rs(self):
        common = (HERE / "nightly-common.sh").read_text()
        self.assertIn("export ANICRAFT_BUILD_CHANNEL ANICRAFT_VERSION ANICRAFT_BUILD_SHA ANICRAFT_BUILD_DATE", common)
        self.assertNotIn("OVERRIDE_VERSION", common)

    def test_every_platform_checks_the_line_the_flavor_prints(self):
        # The contract's line is `<App> <version> (nightly-v1)`: the display name is not in it.
        expected = 'AniCraft $VERSION (nightly-v1)'
        self.assertIn(f'"{expected}"', (HERE / "nightly-common.sh").read_text())
        self.assertIn(f'"{expected}"', (HERE / "linux/nightly.sh").read_text())
        self.assertIn('"AniCraft $VERSION (nightly-v1)"', (HERE / "macos/nightly-verify.sh").read_text())
        self.assertIn("AniCraft $ExpectedVersion (nightly-v1)", (HERE / "windows/nightly.ps1").read_text())
        workflow = (HERE.parent / ".github/workflows/nightly-builds.yml").read_text()
        self.assertIn('"AniCraft $ANICRAFT_VERSION (nightly-v1)"', workflow)
        self.assertIn('"AniCraft $env:ANICRAFT_VERSION (nightly-v1)"', workflow)
        for name in ("nightly-common.sh", "macos/nightly-verify.sh", "windows/nightly.ps1"):
            self.assertNotIn("AniCraft Nightly $", (HERE / name).read_text(), name)

    def test_the_expected_line_is_the_one_the_flavor_composes(self):
        root = HERE.parent
        self.assertIn('BuildFlavor::NightlyV1 => format!("{stable} (nightly-v1)")', (root / "src/build_flavor.rs").read_text())
        # Both queries use the same form: the prepared version, never the display name.
        self.assertIn('writeln!(std::io::stdout(), "AniCraft {}", version_line())', (root / "src/main.rs").read_text())
        self.assertIn('println!("AniCraft {}", version_line())', (root / "src/main.rs").read_text())
        # The window title and the Windows ProductName keep the channel's display name.
        self.assertIn('LaunchIdentity::NightlyV1 => tr_noop!("AniCraft Nightly")', (root / "src/platform.rs").read_text())
        self.assertIn("if ($ProductName -ne 'AniCraft Nightly'", (HERE / "windows/nightly.ps1").read_text())

    def test_the_window_class_is_the_desktop_entry_class(self):
        platform = (HERE.parent / "src/platform.rs").read_text()
        match = re.search(r'pub const APP_ID_NIGHTLY: &str = "([^"]+)";', platform)
        self.assertIsNotNone(match)
        nightly_id = match[1]
        self.assertEqual(nightly_id, "ai.storyteller.anicraft.nightly-v1")
        for name in ("package.sh", "flatpak-bundle.sh"):
            with self.subTest(file=name):
                self.assertIn(nightly_id, self.read(name))
        # the Arch handoff builds the same id from the stable one it rewrites.
        arch = self.read("nightly-arch-tar.sh")
        self.assertIn("app_id=ai.storyteller.anicraft", arch)
        self.assertIn("nightly_id=$app_id.nightly-v1", arch)
        self.assertIn("StartupWMClass=$nightly_id", arch)




class Template(unittest.TestCase):
    """The plain nightly tar's launcher: the canonical shared template, byte for byte.

    It is rendered from craft-common-actions scripts/nightly-launcher.sh.in for this app's registry
    row (entrypoint `anicraft`, binary `anicraft-bin`, identity ai.storyteller.anicraft.nightly);
    the shared nightly producer compares the archive's entry point with that render.
    """

    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.home = self.root / "home with space"
        self.home.mkdir()
        self.tree = self.root / "unpacked"
        self.tree.mkdir()
        self.program = self.tree / "anicraft-bin"
        self.program.write_text(RECORDER, encoding="utf-8")
        self.program.chmod(0o755)
        self.command = self.tree / "anicraft"
        shutil.copyfile(LAUNCHER, self.command)
        self.command.chmod(0o755)

    def recorded(self, *arguments, env=None, command=None):
        environment = {"PATH": "/usr/bin:/bin", "HOME": str(self.home), **(env or {})}
        environment = {key: value for key, value in environment.items() if value is not None}
        return subprocess.run([str(command or self.command), *arguments], capture_output=True, text=True,
                              timeout=30, env=environment)

    def argv(self, *arguments, env=None, command=None):
        result = self.recorded(*arguments, env=env, command=command)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()

    def profile(self, data_home=None):
        return str(Path(data_home or self.home / ".local/share") / IDENTITY)

    def query(self, data_home=None):
        return ["--launch-profile", "nightly-v1", "--profile-root", self.profile(data_home), "--version"]

    def test_the_file_is_the_rendered_template(self):
        source = LAUNCHER.read_text(encoding="utf-8")
        self.assertIn("# Nightly launcher generated by craft-common-actions scripts/nightly-launcher.sh.in.", source)
        self.assertIn('target="$dir/anicraft-bin"', source)
        self.assertIn(f'profile_root="${{XDG_DATA_HOME:-$HOME/.local/share}}/{IDENTITY}"', source)
        self.assertIn('  --launch-profile) exec "$target" "$@" ;;', source)
        for placeholder in ("@BINARY@", "@APP_ID@"):
            self.assertNotIn(placeholder, source, "the template is rendered, not a placeholder")

    def test_a_launch_adds_the_nightly_profile_and_the_files_follow_the_delimiter(self):
        files = ["scene with spaces.anicraft", "--other-name.anicraft"]
        self.assertEqual(self.argv(*files),
                         ["--launch-profile", "nightly-v1", "--profile-root", self.profile(), "--", *files])
        self.assertEqual(self.argv(), ["--launch-profile", "nightly-v1", "--profile-root", self.profile(), "--"])

    def test_the_profile_follows_xdg_data_home(self):
        data_home = self.root / "xdg with space"
        result = self.argv("walk.anicraft", env={"XDG_DATA_HOME": str(data_home)})
        self.assertEqual(result, ["--launch-profile", "nightly-v1", "--profile-root", self.profile(data_home),
                                  "--", "walk.anicraft"])

    def test_both_version_spellings_ask_for_one_plain_version(self):
        for flag in ("--version", "-V"):
            with self.subTest(flag=flag):
                self.assertEqual(self.argv(flag), self.query())
        self.assertEqual(self.query()[-1], "--version", "the template never forwards -V")

    def test_the_launchers_own_profile_line_passes_through_untouched(self):
        # The ArtCraft Launcher starts the entry point with its own profile pair and per-install root;
        # the template hands that line to the GUI unchanged and the app's parser validates it.
        managed = str(self.root / "launcher root")
        for supplied in (["--launch-profile", "nightly-v1", "--profile-root", managed, "--"],
                         ["--launch-profile", "nightly-v1", "--profile-root", managed, "--", "walk.anicraft"],
                         ["--launch-profile"]):
            with self.subTest(supplied=supplied):
                self.assertEqual(self.argv(*supplied), supplied)

    def test_every_other_line_still_gets_the_templates_own_profile(self):
        # Only a line that opens with `--launch-profile` exactly is the Launcher's: a `--profile-root`
        # first, the `=` spelling, or any other argument is wrapped in the fixed profile.
        for arguments in (["--profile-root", str(self.root / "launcher root"), "--"],
                          ["--launch-profile=nightly-v1"], ["--help"]):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.argv(*arguments),
                                 ["--launch-profile", "nightly-v1", "--profile-root", self.profile(), "--", *arguments])

    def test_it_starts_only_the_program_beside_it(self):
        self.program.unlink()
        for arguments in ((), ("--version",), ("walk.anicraft",)):
            with self.subTest(arguments=arguments):
                result = self.recorded(*arguments)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(result.stdout, "")

    def test_its_folder_is_resolved_physically(self):
        linked = self.root / "linked/bin"
        linked.parent.mkdir()
        linked.symlink_to(self.tree)
        self.assertEqual(self.argv("walk.anicraft", command=linked / "anicraft"),
                         ["--launch-profile", "nightly-v1", "--profile-root", self.profile(), "--", "walk.anicraft"])

    def test_no_data_folder_means_no_launch(self):
        result = self.recorded("walk.anicraft", env={"HOME": None})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("HOME", result.stderr)


if __name__ == "__main__":
    unittest.main()
