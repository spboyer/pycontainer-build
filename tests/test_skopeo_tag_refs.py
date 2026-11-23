"""Verify OCI layout reference selection with Skopeo when it is available."""
import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig


def _skopeo(*args):
    return subprocess.run(["skopeo", *args], capture_output=True, timeout=60)


@unittest.skipUnless(shutil.which("skopeo"), "Skopeo is not installed")
class TestSkopeoTagReferences(unittest.TestCase):
    def _build(self, root, primary, tags):
        context = root / "context"
        context.mkdir()
        (context / "app.py").write_text("print('hello')\n")
        layout = root / "layout"
        builder = ImageBuilder(BuildConfig(
            tag=primary,
            tags=tags,
            context_dir=str(context),
            output_dir=str(layout),
            include_deps=False,
            use_cache=False,
        ))
        with patch.object(builder, "_pull_base_image", return_value=([], None)):
            builder.build()
        return builder, layout

    def _assert_resolves(self, reference, builder, layout):
        result = _skopeo("inspect", "--raw", reference)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        digest = "sha256:" + hashlib.sha256(result.stdout).hexdigest()
        self.assertEqual(digest, builder.manifest_digest)
        blob = layout / "blobs" / "sha256" / digest.split(":", 1)[1]
        self.assertEqual(len(result.stdout), blob.stat().st_size)
        print(f"Skopeo resolved {reference} to {digest}")

    def test_every_configured_tag_is_selectable(self):
        cases = [
            ("myapp:latest", ["stable", "v2"],
             ["myapp:latest", "myapp:stable", "myapp:v2"]),
            ("ghcr.io/example/myapp:v1.2.3", ["stable", "v2"],
             ["ghcr.io/example/myapp:v1.2.3", "ghcr.io/example/myapp:stable",
              "ghcr.io/example/myapp:v2"]),
            ("myapp:_dev", ["v1_", "v1..2"],
             ["pycontainer-6d796170703a5f646576", "pycontainer-6d796170703a76315f",
              "pycontainer-6d796170703a76312e2e32"]),
        ]
        for primary, tags, names in cases:
            with self.subTest(primary=primary), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                builder, layout = self._build(root, primary, tags)
                for name, tag in zip(names, [primary.rsplit(":", 1)[1], *tags]):
                    self._assert_resolves(f"oci:{layout}:{name}", builder, layout)
                    self.assertEqual((layout / "refs" / "tags" / tag).read_text(),
                                     builder.manifest_digest)

                copied = root / "copied"
                result = _skopeo("copy", f"oci:{layout}:{names[1]}", f"oci:{copied}:copied")
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                # Skopeo may recompress layers, so compare the unchanged config blob.
                index = json.loads((copied / "index.json").read_text())
                digest = index["manifests"][0]["digest"].split(":", 1)[1]
                manifest = json.loads((copied / "blobs" / "sha256" / digest).read_text())
                self.assertEqual(manifest["config"]["digest"], builder.config_digest)
                print(f"Skopeo copied secondary tag {names[1]}")

                result = _skopeo("inspect", "--raw", f"oci:{layout}")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"more than one image", result.stderr)
                print("Unqualified selection with several references failed as documented: "
                      f"{result.stderr.decode().strip()}")

    def test_single_reference_allows_unqualified_selection(self):
        for primary, name in [
            ("myapp:latest", "myapp:latest"),
            ("myapp:_dev", "pycontainer-6d796170703a5f646576"),
        ]:
            with self.subTest(primary=primary), tempfile.TemporaryDirectory() as temp:
                builder, layout = self._build(Path(temp), primary, [])
                self._assert_resolves(f"oci:{layout}", builder, layout)
                self._assert_resolves(f"oci:{layout}:{name}", builder, layout)


if __name__ == "__main__":
    unittest.main(verbosity=2)