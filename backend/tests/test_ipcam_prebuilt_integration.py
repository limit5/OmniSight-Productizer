"""R5.3 (OP-2164) — ipcam prebuilt rtsp-onvif-server integration scaffold."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# test is backend/tests/ -> parents[2] is the repo root
_SCAFFOLD = (Path(__file__).resolve().parents[2] / "configs" / "skills"
             / "ipcam" / "scaffolds" / "prebuilt_rtsp_onvif_integration.py")


def _load():
    spec = importlib.util.spec_from_file_location("prebuilt_integ", _SCAFFOLD)
    mod = importlib.util.module_from_spec(spec)
    # dataclasses need the module visible in sys.modules during class creation
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_scaffold_file_exists():
    assert _SCAFFOLD.is_file(), _SCAFFOLD


def test_yocto_integration_pins_release_and_profile():
    m = _load()
    art = m.render_integration(
        product="acme-doorcam", soc="rk3588", release_tag="v0.1.0",
        packaging=m.Packaging.YOCTO, manufacturer="Acme", model="DoorCam-1")
    assert art.manifest["server"]["repo"] == "omnisight/rtsp-onvif-server"
    assert art.manifest["server"]["release_tag"] == "v0.1.0"
    assert art.manifest["server"]["packaging"] == "yocto"
    assert art.manifest["hal_profile"] == "rk3588"
    assert art.manifest["hal_profile_verified"] is True
    # config wires the board profile + identity
    assert "profile: rk3588" in art.config_yaml
    assert "manufacturer: Acme" in art.config_yaml
    # yocto glue pins SRCREV
    assert 'SRCREV:pn-rtsp-onvif-server = "v0.1.0"' in art.packaging_glue
    assert "IMAGE_INSTALL" in art.packaging_glue


def test_buildroot_glue():
    m = _load()
    art = m.render_integration(
        product="p", soc="qcs6490", release_tag="v0.1.0",
        packaging=m.Packaging.BUILDROOT, manufacturer="O", model="M")
    assert "BR2_PACKAGE_RTSP_ONVIF_SERVER=y" in art.packaging_glue
    assert 'BR2_PACKAGE_RTSP_ONVIF_SERVER_VERSION="v0.1.0"' \
        in art.packaging_glue
    assert art.manifest["hal_profile"] == "qcs6490"


def test_unknown_soc_falls_back_to_generic_with_warning():
    m = _load()
    art = m.render_integration(
        product="p", soc="some-new-soc", release_tag="v0.1.0",
        packaging=m.Packaging.YOCTO, manufacturer="O", model="M")
    assert art.manifest["hal_profile"] == "generic-v4l2"
    assert art.manifest["hal_profile_verified"] is False
    assert any("generic-v4l2" in line and "WARNING" in line
               for line in art.smoke_checklist)


def test_validation():
    m = _load()
    with pytest.raises(ValueError):
        m.render_integration(
            product="", soc="rk3588", release_tag="v0.1.0",
            packaging=m.Packaging.YOCTO, manufacturer="O", model="M")
    with pytest.raises(ValueError):
        m.render_integration(
            product="p", soc="rk3588", release_tag="",
            packaging=m.Packaging.YOCTO, manufacturer="O", model="M")
    with pytest.raises(TypeError):
        m.render_integration(
            product="p", soc="rk3588", release_tag="v0.1.0",
            packaging="yocto", manufacturer="O", model="M")
