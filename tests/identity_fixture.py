"""Invented shared app configuration for offline packaging tests."""
from pathlib import Path


def reference(directory):
    filename = Path(directory) / "const.py"
    modulus = (2 ** 521 - 1) * (2 ** 607 - 1)
    filename.write_text("APPID='exampleappid0000'\nAPPKEY='example_application_key'\nRSA_N=" + str(modulus) + "\nRSA_E=65537\n")
    return filename
