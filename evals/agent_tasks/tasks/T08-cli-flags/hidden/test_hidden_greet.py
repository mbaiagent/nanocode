import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def run(*args):
    return subprocess.run([sys.executable, os.path.join(HERE, "greet.py"), *args], capture_output=True, text=True)


class GreetTest(unittest.TestCase):
    def test_default(self):
        self.assertEqual(run("Ann").stdout, "Hello, Ann!\n")

    def test_shout(self):
        self.assertEqual(run("Ann", "--shout").stdout, "HELLO, ANN!\n")

    def test_times(self):
        self.assertEqual(run("--times", "3", "Bo").stdout, "Hello, Bo!\n" * 3)
        self.assertEqual(run("Bo", "--times", "2", "--shout").stdout, "HELLO, BO!\n" * 2)

    def test_invalid_times(self):
        self.assertNotEqual(run("Bo", "--times", "0").returncode, 0)
        self.assertNotEqual(run("Bo", "--times", "x").returncode, 0)
