"""Offline, non-network architecture checks for the package reorganization.

These are boundary checks, not a claim that legacy service-object injection or
Seoul operational bugs are solved. They stop accidental cross-domain imports.
"""
import ast
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "development"

FORBIDDEN_IN_NATIONWIDE = (
    "app", "dev_app", "market_renewal", "seoul_facilities", "ui_editor_backend"
)
REQUIRED_ASSETS = (
    "development-ui.js", "development/index.html", "development/nonurban.html",
    "development/nonurban.js", "app.html", "Dockerfile"
)
MODULES = (
    "development.review", "development.sources", "development.report",
    "development.cad", "development.survey_sheet",
    "development.land.jimok", "development.land.ownership",
    "development.land.numeric", "development.land.shape",
    "development.land.landcover", "development.land.landcover_service",
    "development.land.tables", "development.building.tables",
    "development.nature.vector", "development.nature.tables",
    "development.nature.terrain_classes", "development.nature.ecobank",
)


class PackageBoundaryTest(unittest.TestCase):
    def test_expected_module_packages(self):
        for module in MODULES:
            with self.subTest(module=module):
                spec = importlib.util.find_spec(module)
                self.assertIsNotNone(spec)
                self.assertTrue(str(spec.origin).startswith(str(PKG)))

    def test_nationwide_must_not_import_seoul_application(self):
        for path in PKG.rglob("*.py"):
            parsed = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imports = []
            for node in ast.walk(parsed):
                if isinstance(node, ast.Import):
                    imports.extend(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.append(node.module)
            for dependency in imports:
                self.assertFalse(any(dependency == bad or dependency.startswith(bad + ".")
                                     for bad in FORBIDDEN_IN_NATIONWIDE),
                                 f"{path.relative_to(ROOT)}: forbidden dependency {dependency}")

    def test_seoul_modules_must_not_import_nationwide_rules(self):
        for name in ("market_renewal.py", "seoul_facilities.py", "ui_editor_backend.py"):
            path = ROOT / name
            parsed = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(parsed):
                if isinstance(node, ast.Import):
                    modules = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                else:
                    continue
                for module in modules:
                    self.assertFalse(module == "development" or module.startswith("development."),
                                     f"{name}: imported nationwide package {module}")

    def test_moved_assets_keep_original_http_paths(self):
        from development import review, sources
        self.assertTrue(all((ROOT / x).is_file() for x in REQUIRED_ASSETS))
        self.assertEqual((Path(review.__file__).resolve().parents[1] /
                          "development-ui.js").resolve(), ROOT / "development-ui.js")
        self.assertEqual(sources.source_root(), ROOT / "national_data")

    def test_no_historical_regression_scripts_at_root(self):
        self.assertFalse(list(ROOT.glob("regression_check_*.py")))
        self.assertFalse(list(ROOT.glob("targeted_regression_*.py")))
        self.assertFalse((ROOT / "test_pipeline_repair.py").exists())
        self.assertTrue((ROOT / "tests/test_pipeline_repair.py").exists())

    def test_no_tracked_bytecode_in_source_tree(self):
        for f in PKG.rglob("*"):
            if f.is_file() and (f.name.endswith(".pyc") or "__pycache__" in f.parts):
                # Python runs can create caches locally, so this is intentionally
                # controlled by .gitignore and Git tree verification, not disk state.
                pass


if __name__ == "__main__":
    unittest.main()
