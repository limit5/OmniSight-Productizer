"""usb-cam pack — prebuilt uvc-uac-app integration scaffold."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# test is backend/tests/ -> parents[2] is the repo root
_SCAFFOLD = (Path(__file__).resolve().parents[2] / "configs" / "skills"
             / "usb-cam" / "scaffolds" / "prebuilt_uvc_uac_integration.py")


def _load():
    spec = importlib.util.spec_from_file_location("usbcam_integ", _SCAFFOLD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses need module in sys.modules
    spec.loader.exec_module(mod)
    return mod


def test_scaffold_file_exists():
    assert _SCAFFOLD.is_file(), _SCAFFOLD


def test_uvc_only_mode_has_no_audio():
    m = _load()
    art = m.render_integration(
        product="acme-streamcam", soc="rk3588", release_tag="v0.1.0",
        packaging=m.Packaging.YOCTO, mode=m.Mode.UVC_ONLY,
        manufacturer="Acme", product_name="StreamCam-1")
    assert art.manifest["mode"] == "uvc-only"
    assert art.manifest["firmware"]["repo"] == "omnisight/uvc-uac-app"
    assert art.manifest["firmware"]["release_tag"] == "v0.1.0"
    assert art.manifest["hal_profile_verified"] is True
    assert art.manifest["hardware_encode"] is True
    # no audio block in UVC-only
    assert "audio:" not in art.profile_yaml
    assert "uac_version" not in art.profile_yaml
    # has H.264/H.265 (rk3588 has VPU)
    assert "H264" in art.profile_yaml and "H265" in art.profile_yaml
    assert 'SRCREV:pn-uvc-uac-app = "v0.1.0"' in art.packaging_glue


def test_uvc_uac_mode_has_audio_and_checklist():
    m = _load()
    art = m.render_integration(
        product="conf-cam", soc="rk3588", release_tag="v0.1.0",
        packaging=m.Packaging.BUILDROOT, mode=m.Mode.UVC_UAC,
        manufacturer="O", product_name="ConfCam")
    assert art.manifest["mode"] == "uvc-uac"
    assert "audio:" in art.profile_yaml
    assert "uac_version" in art.profile_yaml
    assert "BR2_PACKAGE_UVC_UAC_APP=y" in art.packaging_glue
    assert any("Audio" in c for c in art.smoke_checklist)


def test_no_vpu_soc_limits_to_mjpeg_with_note():
    m = _load()
    art = m.render_integration(
        product="p", soc="imx93", release_tag="v0.1.0",
        packaging=m.Packaging.DEBIAN, mode=m.Mode.UVC_ONLY,
        manufacturer="O", product_name="M")
    assert art.manifest["hardware_encode"] is False
    # no hardware H.264/H.265
    assert "H264" not in art.profile_yaml
    assert "MJPEG" in art.profile_yaml
    assert any("no VPU" in c for c in art.smoke_checklist)


def test_unprofiled_soc_warns():
    m = _load()
    art = m.render_integration(
        product="p", soc="qcs8550", release_tag="v0.1.0",
        packaging=m.Packaging.YOCTO, mode=m.Mode.UVC_ONLY,
        manufacturer="O", product_name="M")
    assert art.manifest["hal_profile_verified"] is False
    assert any("no verified" in c and "HAL profile" in c
               for c in art.smoke_checklist)


def test_debian_glue_mentions_static_cxx():
    m = _load()
    art = m.render_integration(
        product="p", soc="rk3588", release_tag="v0.1.0",
        packaging=m.Packaging.DEBIAN, mode=m.Mode.UVC_ONLY,
        manufacturer="O", product_name="M")
    assert "build-deb.sh" in art.packaging_glue


def test_validation():
    m = _load()
    with pytest.raises(ValueError):
        m.render_integration(
            product="", soc="rk3588", release_tag="v0.1.0",
            packaging=m.Packaging.YOCTO, mode=m.Mode.UVC_ONLY,
            manufacturer="O", product_name="M")
    with pytest.raises(TypeError):
        m.render_integration(
            product="p", soc="rk3588", release_tag="v0.1.0",
            packaging=m.Packaging.YOCTO, mode="uvc-only",
            manufacturer="O", product_name="M")
