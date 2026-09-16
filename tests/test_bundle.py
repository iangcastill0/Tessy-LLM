"""Tests for frozen-app resource resolution.

These simulate a PyInstaller bundle by setting sys.frozen/sys._MEIPASS, so they
run in an ordinary checkout without building anything.
"""

from __future__ import annotations

import os
import sys

import pytest

from tessy import bundle


@pytest.fixture
def fake_bundle(tmp_path, monkeypatch):
    """Make the process look frozen, with tmp_path as the resource directory."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    return tmp_path


class TestNotFrozen:
    """A source checkout must behave exactly as before."""

    def test_not_frozen_by_default(self):
        assert bundle.is_frozen() is False

    def test_no_resource_dir(self):
        assert bundle.resource_dir() is None

    def test_no_bundled_tesseract(self):
        assert bundle.bundled_tesseract() is None

    def test_no_bundled_tessdata(self):
        assert bundle.bundled_tessdata() is None

    def test_subprocess_env_is_untouched(self, monkeypatch):
        monkeypatch.delenv("TESSDATA_PREFIX", raising=False)
        assert "TESSDATA_PREFIX" not in bundle.subprocess_env()


class TestBundledTessdata:
    def test_found_when_models_are_present(self, fake_bundle):
        tessdata = fake_bundle / "tessdata"
        tessdata.mkdir()
        (tessdata / "eng.traineddata").write_bytes(b"x")
        assert bundle.bundled_tessdata() == tessdata

    def test_empty_directory_is_not_accepted(self, fake_bundle):
        (fake_bundle / "tessdata").mkdir()
        assert bundle.bundled_tessdata() is None

    def test_points_at_the_tessdata_dir_itself(self, fake_bundle, monkeypatch):
        """Tesseract 5 wants TESSDATA_PREFIX to be the tessdata dir.

        Naming the parent makes it report a language called "tessdata/eng",
        which then never matches `-l eng`.
        """
        tessdata = fake_bundle / "tessdata"
        tessdata.mkdir()
        (tessdata / "eng.traineddata").write_bytes(b"x")
        monkeypatch.delenv("TESSDATA_PREFIX", raising=False)

        env = bundle.subprocess_env()
        assert env["TESSDATA_PREFIX"] == str(tessdata)
        assert not env["TESSDATA_PREFIX"].endswith(f"{os.sep}tessdata{os.sep}tessdata")

    def test_operator_setting_wins(self, fake_bundle, monkeypatch):
        tessdata = fake_bundle / "tessdata"
        tessdata.mkdir()
        (tessdata / "eng.traineddata").write_bytes(b"x")
        monkeypatch.setenv("TESSDATA_PREFIX", "/operators/own/tessdata")

        assert bundle.subprocess_env()["TESSDATA_PREFIX"] == "/operators/own/tessdata"


class TestBundledBinary:
    def _make_binary(self, root):
        folder = root / "tesseract"
        folder.mkdir()
        binary = folder / bundle.TESSERACT_EXE
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        return binary

    def test_found_when_executable(self, fake_bundle):
        binary = self._make_binary(fake_bundle)
        assert bundle.bundled_tesseract() == binary

    def test_ignored_when_not_executable(self, fake_bundle):
        binary = self._make_binary(fake_bundle)
        binary.chmod(0o644)
        assert bundle.bundled_tesseract() is None

    def test_missing_binary(self, fake_bundle):
        assert bundle.bundled_tesseract() is None

    @pytest.mark.skipif(os.name == "nt", reason="LD_LIBRARY_PATH is POSIX-only")
    def test_library_path_points_at_the_bundled_libs(self, fake_bundle, monkeypatch):
        """Without this the bundled binary would load the host's libtesseract."""
        binary = self._make_binary(fake_bundle)
        monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
        assert bundle.subprocess_env()["LD_LIBRARY_PATH"] == str(binary.parent)

    @pytest.mark.skipif(os.name == "nt", reason="LD_LIBRARY_PATH is POSIX-only")
    def test_existing_library_path_is_preserved_after_ours(self, fake_bundle, monkeypatch):
        binary = self._make_binary(fake_bundle)
        monkeypatch.setenv("LD_LIBRARY_PATH", "/existing/libs")
        value = bundle.subprocess_env()["LD_LIBRARY_PATH"]
        assert value == f"{binary.parent}{os.pathsep}/existing/libs"


class TestDiscoveryOrder:
    def test_explicit_override_beats_the_bundle(self, fake_bundle, monkeypatch, tmp_path):
        """An operator pointing at their own build must always win."""
        from tessy.ocr import find_tesseract

        folder = fake_bundle / "tesseract"
        folder.mkdir()
        packaged = folder / bundle.TESSERACT_EXE
        packaged.write_text("#!/bin/sh\n")
        packaged.chmod(0o755)

        theirs = tmp_path / "their_tesseract"
        theirs.write_text("#!/bin/sh\n")
        theirs.chmod(0o755)
        monkeypatch.setenv("TESSERACT_BIN", str(theirs))

        assert find_tesseract() == str(theirs)

    def test_bundle_beats_path(self, fake_bundle, monkeypatch):
        from tessy.ocr import find_tesseract

        folder = fake_bundle / "tesseract"
        folder.mkdir()
        packaged = folder / bundle.TESSERACT_EXE
        packaged.write_text("#!/bin/sh\n")
        packaged.chmod(0o755)

        monkeypatch.delenv("TESSERACT_BIN", raising=False)
        monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/tesseract")

        assert find_tesseract() == str(packaged)


class TestDescribe:
    def test_reports_bundle_state(self, fake_bundle):
        tessdata = fake_bundle / "tessdata"
        tessdata.mkdir()
        (tessdata / "eng.traineddata").write_bytes(b"x")

        described = bundle.describe()
        assert described["frozen"] == "True"
        assert described["bundled_tessdata"] == str(tessdata)
        assert described["bundled_tesseract"] is None

    def test_reports_nothing_when_not_frozen(self):
        described = bundle.describe()
        assert described["frozen"] == "False"
        assert described["resources"] is None
