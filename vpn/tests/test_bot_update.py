import pathlib
import subprocess
import unittest


class BotUpdateSafetyTests(unittest.TestCase):
    def test_bot_only_path_is_before_infrastructure_changes(self):
        path = pathlib.Path(__file__).resolve().parents[1] / "install.sh"
        script = path.read_text()
        block = script.split("if [[ ${1:-} == --bot-only", 1)[1].split("\nask()", 1)[0]
        self.assertIn("systemctl stop isaho-bot", block)
        self.assertIn("previous code restored", block)
        self.assertIn("exit 0", block)
        for forbidden in ("systemctl restart xray", "ufw ", "sysctl ", "apt-get ",
                          'source "$ENV_FILE"', 'cat >"$ENV_FILE"', "install-release.sh"):
            self.assertNotIn(forbidden, block)
        self.assertLess(script.index("--bot-only"), script.index("apt-get update"))

    def test_installer_shell_syntax(self):
        path = pathlib.Path(__file__).resolve().parents[1] / "install.sh"
        subprocess.run(["bash", "-n", str(path)], check=True)


if __name__ == "__main__":
    unittest.main()
