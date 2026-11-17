"""Static, offline checks for the DeepSeek Harness image installation."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")


class DshImageTests(unittest.TestCase):
    def test_uses_node_24_for_dsh_engine_compatibility(self):
        self.assertRegex(DOCKERFILE, r"(?m)^FROM node:24-bookworm$")
        self.assertIn("requires Node.js 24", README)

    def test_installs_pinned_dsh_in_existing_global_npm_layer(self):
        self.assertRegex(DOCKERFILE, r"(?m)^ARG DSH_VERSION=0\.2\.2-rc\.1$")
        npm_install = re.search(r"(?ms)^RUN npm_config_proxy=.*?^\s*npm install -g .*?(?=\n\S|\Z)", DOCKERFILE)
        self.assertIsNotNone(npm_install)
        self.assertIn("@deepseek-ai/dsh@${DSH_VERSION}", npm_install.group(0))
        self.assertIn("dsh --version", npm_install.group(0))

    def test_publishes_dsh_version_label(self):
        self.assertRegex(
            DOCKERFILE,
            r'(?m)^LABEL org\.opencontainers\.image\.dsh\.version="\$\{DSH_VERSION\}"$',
        )

    def test_does_not_bake_dsh_credentials(self):
        forbidden = re.compile(r"(?i)(?:DEEPSEEK_API_KEY|DSH_TOKEN|DEEPSEEK_TOKEN)")
        self.assertIsNone(forbidden.search(DOCKERFILE))
        normalized_readme = " ".join(README.split())
        self.assertIn("Credentials are not baked into the image", normalized_readme)
        self.assertIn("does not configure authentication", normalized_readme)


if __name__ == "__main__":
    unittest.main()
