import importlib
import unittest


class TestClientSmoke(unittest.TestCase):
    def test_entrypoint_exposes_main(self) -> None:
        module = importlib.import_module("client_template")
        self.assertTrue(hasattr(module, "main"))


if __name__ == "__main__":
    unittest.main()
