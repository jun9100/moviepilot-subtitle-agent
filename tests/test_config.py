from __future__ import annotations

from app.config import Settings


def test_cookiecloud_new_names_take_effect():
    settings = Settings(
        cookiecloud_url="https://example.com/cookiecloud",
        cookiecloud_key="new_key",
        cookiecloud_password="new_pwd",
        cookiecloud_sync_interval_seconds=600,
    )

    assert settings.effective_cookiecloud_url == "https://example.com/cookiecloud"
    assert settings.effective_cookiecloud_key == "new_key"
    assert settings.effective_cookiecloud_password == "new_pwd"
    assert settings.effective_cookiecloud_sync_interval_seconds == 600


def test_cookiecloud_legacy_names_still_work():
    settings = Settings(
        subhd_cookiecloud_url="https://legacy.example.com/cookiecloud",
        subhd_cookiecloud_key="legacy_key",
        subhd_cookiecloud_password="legacy_pwd",
        subhd_cookiecloud_sync_interval_seconds=900,
    )

    assert settings.effective_cookiecloud_url == "https://legacy.example.com/cookiecloud"
    assert settings.effective_cookiecloud_key == "legacy_key"
    assert settings.effective_cookiecloud_password == "legacy_pwd"
    assert settings.effective_cookiecloud_sync_interval_seconds == 900


def test_cookiecloud_new_names_override_legacy_names():
    settings = Settings(
        cookiecloud_url="https://new.example.com/cookiecloud",
        cookiecloud_key="new_key",
        cookiecloud_password="new_pwd",
        cookiecloud_sync_interval_seconds=300,
        subhd_cookiecloud_url="https://legacy.example.com/cookiecloud",
        subhd_cookiecloud_key="legacy_key",
        subhd_cookiecloud_password="legacy_pwd",
        subhd_cookiecloud_sync_interval_seconds=900,
    )

    assert settings.effective_cookiecloud_url == "https://new.example.com/cookiecloud"
    assert settings.effective_cookiecloud_key == "new_key"
    assert settings.effective_cookiecloud_password == "new_pwd"
    assert settings.effective_cookiecloud_sync_interval_seconds == 300


def test_captcha_ocr_settings_normalization():
    settings = Settings(
        enable_captcha_ocr=True,
        captcha_ocr_endpoint="  http://ocr.local/solve  ",
        captcha_ocr_timeout_seconds="3",
        captcha_ocr_auto_max_attempts="5",
        captcha_ocr_min_confidence="0.75",
    )

    assert settings.enable_captcha_ocr is True
    assert settings.effective_captcha_ocr_endpoint == "http://ocr.local/solve"
    assert settings.captcha_ocr_timeout_seconds == 3
    assert settings.captcha_ocr_auto_max_attempts == 5
    assert settings.captcha_ocr_min_confidence == 0.75


def test_captcha_ocr_auto_max_attempts_default_is_five():
    settings = Settings()
    assert settings.captcha_ocr_auto_max_attempts == 5
