"""Verify OCI layout reference selection with Skopeo when it is available."""
import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig


@unittest.skipUnless(shutil.which("skopeo"), "Skopeo is not installed")
class TestSkopeoTagReferences(unittest.TestCase):
    def test_primary_reference_and_additional_tag(self):
        for primary in ("myapp:latest", "ghcr.io/example/myapp:v1.2.3"):
            with self.subTest(primary=primary), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                context = root / "context"
                context.mkdir()
                (context / "app.py").write_text("print('hello')\n")
                layout = root / "layout"
                builder = ImageBuilder(BuildConfig(
                    tag=primary,
                    tags=["stable"],
                    context_dir=str(context),
                    output_dir=str(layout),
                    include_deps=False,
                    use_cache=False,
                ))
                with patch.object(builder, "_pull_base_image", return_value=([], None)):
                    builder.build()

                for reference in (f"oci:{layout}:{primary}", f"oci:{layout}"):
                    result = subprocess.run(
                        ["skopeo", "inspect", "--raw", reference],
                        check=True, capture_output=True, timeout=30,
                    )
                    digest = "sha256:" + hashlib.sha256(result.stdout).hexdigest()
                    self.assertEqual(digest, builder.manifest_digest)
                    print(f"Skopeo resolved {reference} to {digest}")

                self.assertEqual((layout / "refs" / "tags" / "stable").read_text(),
                                 builder.manifest_digest)
                for extra in ("stable", builder.config.all_tags()[1]):
                    result = subprocess.run(
                        ["skopeo", "inspect", "--raw", f"oci:{layout}:{extra}"],
                        capture_output=True, timeout=30,
                    )
                    if result.returncode:
                        self.assertIn(b"no descriptor found for reference", result.stderr)
                        print(f"Additional tag {extra} is not selectable: "
                              f"{result.stderr.decode().strip()}")
                    else:
                        digest = "sha256:" + hashlib.sha256(result.stdout).hexdigest()
                        self.assertEqual(digest, builder.manifest_digest)
                        print(f"Additional tag {extra} resolved to {digest}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
