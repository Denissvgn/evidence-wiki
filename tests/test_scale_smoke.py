import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS = REPO_ROOT / "tools"
RUN_SCALE = os.environ.get("EVIDENCE_WIKI_RUN_SCALE") == "1"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SCALE = load_module("scale_benchmark", TOOLS / "scale_benchmark.py")


class ScaleBenchmarkToolTests(unittest.TestCase):
    def test_benchmark_tool_reports_pipeline_counts_and_indexed_query(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = SCALE.run_benchmark(
                SCALE.BenchmarkConfig(
                    sources=3,
                    wiki_pages=4,
                    tmpdir=Path(tmpdir),
                    keep_workspace=False,
                )
            )

        self.assertEqual("1.0", result["schema_version"])
        self.assertEqual(3, result["counts"]["source_records"])
        self.assertEqual(3, result["counts"]["normalized_records"])
        self.assertEqual(4, result["counts"]["wiki_pages"])
        self.assertEqual(7, result["counts"]["indexed_documents"])
        self.assertEqual([], result["warnings"])
        self.assertEqual(0, result["lint"]["high_issues"])
        self.assertTrue(result["status"]["smoke_ok"])
        self.assertEqual("complete", result["status"]["verdict"])
        self.assertTrue(result["status"]["cache_present"])
        self.assertEqual("sqlite_fts5", result["query"]["engine"])
        self.assertGreater(result["query"]["result_count"], 0)
        self.assertGreater(result["resources"]["peak_memory_bytes"], 0)
        self.assertGreater(result["resources"]["output_bytes"], 0)
        self.assertIsNone(result["release_budget"])
        for timing in (
            "inventory",
            "normalization",
            "lint",
            "workspace_status",
            "workspace_status_cached",
            "index_build",
            "indexed_query",
            "total",
        ):
            self.assertIn(timing, result["timings_seconds"])
            self.assertGreaterEqual(result["timings_seconds"][timing], 0.0)

    def test_release_benchmark_profiles_freeze_scale_memory_output_and_time_thresholds(self):
        standard = SCALE.BENCHMARK_PROFILES["standard"]
        near_partition = SCALE.BENCHMARK_PROFILES["near-partition"]

        self.assertEqual((1000, 2000), (standard.sources, standard.wiki_pages))
        self.assertEqual((1250, 2500), (near_partition.sources, near_partition.wiki_pages))
        for profile in (standard, near_partition):
            thresholds = profile.thresholds.as_dict()
            self.assertGreater(thresholds["peak_memory_bytes"], 0)
            self.assertGreater(thresholds["output_bytes"], 0)
            self.assertGreater(thresholds["total_seconds"], 0.0)
            self.assertEqual(
                {
                    "index_build_seconds",
                    "indexed_query_seconds",
                    "inventory_seconds",
                    "lint_seconds",
                    "normalization_seconds",
                    "output_bytes",
                    "peak_memory_bytes",
                    "total_seconds",
                    "workspace_status_cached_seconds",
                    "workspace_status_seconds",
                },
                set(thresholds),
            )

    def test_release_budget_violation_is_reported_as_no_ship(self):
        profile = SCALE.BENCHMARK_PROFILES["standard"]
        timings = {
            "inventory": 0.1,
            "normalization": 0.1,
            "lint": 0.1,
            "workspace_status": 0.1,
            "workspace_status_cached": 0.1,
            "index_build": 0.1,
            "indexed_query": 0.1,
            "total": profile.thresholds.total_seconds + 1.0,
        }

        result = SCALE.evaluate_release_budget(
            profile,
            timings,
            peak_memory_bytes=1,
            output_bytes=1,
        )

        self.assertEqual("no_ship", result["verdict"])
        self.assertEqual(["total_seconds"], [item["metric"] for item in result["violations"]])


@unittest.skipUnless(RUN_SCALE, "set EVIDENCE_WIKI_RUN_SCALE=1 to run optional scale smoke tests")
class OptionalScaleSmokeTests(unittest.TestCase):
    PROFILE = SCALE.BENCHMARK_PROFILES["standard"]

    def test_benchmark_pipeline_handles_large_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = SCALE.run_benchmark(
                SCALE.config_for_profile(
                    "standard",
                    tmpdir=Path(tmpdir),
                    keep_workspace=False,
                )
            )

        self.assertEqual(self.PROFILE.sources, result["counts"]["source_records"])
        self.assertEqual(self.PROFILE.sources, result["counts"]["normalized_records"])
        self.assertEqual(self.PROFILE.wiki_pages, result["counts"]["wiki_pages"])
        self.assertEqual(self.PROFILE.sources + self.PROFILE.wiki_pages, result["counts"]["indexed_documents"])
        self.assertEqual([], result["warnings"])
        self.assertEqual(0, result["lint"]["high_issues"])
        self.assertEqual("complete", result["status"]["verdict"])
        self.assertGreater(result["query"]["result_count"], 0)
        self.assertEqual("pass", result["release_budget"]["verdict"], result["release_budget"]["violations"])


if __name__ == "__main__":
    unittest.main()
