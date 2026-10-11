#!/usr/bin/env python3
"""Tests for the Omka-Patcher recipe (port/yuppiepsycho/tools/recipe.py).

Needs the Omka-Patcher engine: OMKA_PATCHER_SRC, ../../Omka-Patcher (ports live in
~/Projects/MyPorts, the engine in ~/Projects), or build/omka-patcher as fetched by
build.sh; skipped otherwise.

    python3 -m unittest discover -s tests -p 'test_*.py'
"""
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(REPO, "port", "yuppiepsycho", "tools")
ENGINE = next((d for d in (os.environ.get("OMKA_PATCHER_SRC"), os.path.join(REPO, "..", "..", "Omka-Patcher"),
                           os.path.join(REPO, "build", "omka-patcher"))
               if d and os.path.isfile(os.path.join(d, "omkapatcher.py"))), None)
if ENGINE:
    sys.path.insert(0, ENGINE)
    import omkapatcher  # noqa: E402


@unittest.skipIf(ENGINE is None, "Omka-Patcher not found")
class RecipeTests(unittest.TestCase):
    def setUp(self):
        self.recipe = omkapatcher.Recipe(os.path.join(TOOLS, "recipe.py"))
        self.makers = omkapatcher.load_devices(os.path.join(ENGINE, "devices.json"))

    def profile(self, name):
        return self.recipe.profile(omkapatcher.find_device(self.makers, name))

    def test_profiles(self):
        self.assertEqual(self.profile("Anbernic RG40XX H"), {"textures": "16bit", "videos": "640x480"})
        self.assertEqual(self.profile("Anbernic RG353VS"), {"textures": "16bit", "videos": "640x480"})
        self.assertEqual(self.profile("Anbernic RG353V"), {"textures": "full", "videos": "640x480"})
        self.assertEqual(self.profile("Anbernic RG552"), {"textures": "full", "videos": "960x540"})
        self.assertEqual(self.profile("Retroid Pocket 5"), {"textures": "full", "videos": "full"})
        self.assertEqual(self.profile("Hardkernel ODROID-GO Advance"), {"textures": "16bit", "videos": "480x320"})

    def test_every_device(self):
        for _maker, devices in self.makers:
            for dev in devices:
                p = self.recipe.profile(dev)
                self.assertIn(p["textures"], ("16bit", "full"))
                self.assertIn(p["videos"], [v for v, _l in self.recipe.choices("videos", dev, p["videos"])])

    def test_detected_handheld(self):
        rg40 = omkapatcher.Device("", "RG40XX H", 970, (640, 480))
        self.assertEqual(self.recipe.profile(rg40), {"textures": "16bit", "videos": "640x480"})
        rp5 = omkapatcher.Device("", "RP5", 7600, (1920, 1080), soc={"name": "", "fast": True})
        self.assertEqual(self.recipe.profile(rp5), {"textures": "full", "videos": "full"})

    def test_warning_and_mac_build(self):
        rg40 = omkapatcher.find_device(self.makers, "Anbernic RG40XX H")
        self.assertTrue(self.recipe.warning(rg40, {"textures": "full", "videos": "full"}))
        self.assertFalse(self.recipe.warning(rg40, {"textures": "16bit", "videos": "full"}))
        mac = os.path.join("x", "Yuppie Psycho.app", "Contents", "MacOS")
        self.assertIn("macOS build", self.recipe.check_game(mac))
        self.assertIsNone(self.recipe.check_game(os.path.join("x", "Yuppie Psycho")))

    def test_source_tree_is_refused(self):
        ctx = omkapatcher.Context(self.recipe, os.path.dirname(TOOLS))
        self.assertIn("build.sh", self.recipe.check(ctx))


if __name__ == "__main__":
    unittest.main()
