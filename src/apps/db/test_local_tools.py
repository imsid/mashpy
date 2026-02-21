"""Unit tests for db local tools."""

from __future__ import annotations

import json
import ast
import tempfile
import unittest
from pathlib import Path

from apps.db.local_tools import build_analyst_tools


class CompileMetricConfigsToSQLTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.metrics_root = (
            self.root / "src" / "apps" / "db" / "metrics_layer" / "marketing"
        )
        (self.metrics_root / "sources").mkdir(parents=True, exist_ok=True)
        (self.metrics_root / "metrics").mkdir(parents=True, exist_ok=True)
        self._write_source_config()
        self._write_metric_configs()

        tools = build_analyst_tools(workspace_root=self.root)
        self.tool = next(tool for tool in tools if tool.name == "compile_metric_configs_to_sql")

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_compile_simple_metric_default_limit(self) -> None:
        result = self.tool.execute(
            {
                "dataset_id": "marketing",
                "metric_names": ["spend_total"],
            }
        )

        self.assertFalse(result.is_error)
        payload = json.loads(result.content)

        self.assertEqual(list(payload.keys()), ["dataset_id", "count", "plans"])
        self.assertEqual(payload["dataset_id"], "marketing")
        self.assertEqual(payload["count"], 1)

        plan = payload["plans"][0]
        self.assertEqual(
            list(plan.keys()),
            [
                "metric_name",
                "source_id",
                "table_ref",
                "sql",
                "dimensions",
                "filters",
                "order_by",
                "limit",
                "warnings",
            ],
        )
        self.assertEqual(plan["metric_name"], "spend_total")
        self.assertIn("marketing.campaign_ads", plan["table_ref"])
        self.assertIn("SUM(spend) AS metric_value", plan["sql"])
        self.assertIn("LIMIT 100", plan["sql"])
        self.assertEqual(plan["limit"], 100)

    def test_compile_ratio_metric_with_referenced_metric_ids(self) -> None:
        result = self.tool.execute(
            {
                "dataset_id": "marketing",
                "metric_names": ["ctr"],
                "dimensions": ["campaign_id"],
                "order_by": [{"field": "metric_value", "direction": "DESC"}],
            }
        )

        self.assertFalse(result.is_error)
        payload = json.loads(result.content)
        sql = payload["plans"][0]["sql"]
        self.assertIn("SAFE_DIVIDE", sql)
        self.assertIn("SUM(clicks)", sql)
        self.assertIn("SUM(impressions)", sql)
        self.assertIn("GROUP BY campaign_id", sql)
        self.assertIn("ORDER BY metric_value DESC", sql)

    def test_compile_ratio_metric_with_embedded_metric_expression(self) -> None:
        result = self.tool.execute(
            {
                "dataset_id": "marketing",
                "metric_names": ["cpm"],
            }
        )

        self.assertFalse(result.is_error)
        payload = json.loads(result.content)
        sql = payload["plans"][0]["sql"]
        self.assertIn("SAFE_DIVIDE", sql)
        self.assertIn("SUM(spend)", sql)
        self.assertIn("/ 1000", sql)

    def test_compile_multiple_metrics_preserves_input_order(self) -> None:
        result = self.tool.execute(
            {
                "dataset_id": "marketing",
                "metric_names": ["clicks_total", "spend_total"],
            }
        )

        self.assertFalse(result.is_error)
        payload = json.loads(result.content)
        self.assertEqual(payload["count"], 2)
        self.assertEqual(
            [plan["metric_name"] for plan in payload["plans"]],
            ["clicks_total", "spend_total"],
        )

    def test_rejects_unknown_metric(self) -> None:
        result = self.tool.execute(
            {
                "dataset_id": "marketing",
                "metric_names": ["does_not_exist"],
            }
        )

        self.assertTrue(result.is_error)
        payload = json.loads(result.content)
        self.assertEqual(payload["status"], "compile_failed")
        self.assertEqual(payload["errors"][0]["metric_name"], "does_not_exist")

    def test_rejects_unknown_source(self) -> None:
        result = self.tool.execute(
            {
                "dataset_id": "marketing",
                "metric_names": ["broken_source"],
            }
        )

        self.assertTrue(result.is_error)
        payload = json.loads(result.content)
        self.assertEqual(payload["status"], "compile_failed")
        self.assertEqual(payload["errors"][0]["metric_name"], "broken_source")
        self.assertIn("source config file not found", payload["errors"][0]["error"])

    def test_rejects_invalid_dimension(self) -> None:
        result = self.tool.execute(
            {
                "dataset_id": "marketing",
                "metric_names": ["spend_total"],
                "dimensions": ["not_a_dimension"],
            }
        )

        self.assertTrue(result.is_error)
        payload = json.loads(result.content)
        self.assertEqual(payload["status"], "compile_failed")
        self.assertIn("not found in source", payload["errors"][0]["error"])

    def test_rejects_invalid_date_range(self) -> None:
        result = self.tool.execute(
            {
                "dataset_id": "marketing",
                "metric_names": ["spend_total"],
                "date_range": {
                    "dimension": "start_date",
                    "start": "2026/01/01",
                },
            }
        )

        self.assertTrue(result.is_error)
        payload = json.loads(result.content)
        self.assertEqual(payload["status"], "compile_failed")
        self.assertIsNone(payload["errors"][0]["metric_name"])
        self.assertIn("YYYY-MM-DD", payload["errors"][0]["error"])

    def test_rejects_invalid_order_field(self) -> None:
        result = self.tool.execute(
            {
                "dataset_id": "marketing",
                "metric_names": ["spend_total"],
                "order_by": [{"field": "campaign_id", "direction": "ASC"}],
            }
        )

        self.assertTrue(result.is_error)
        payload = json.loads(result.content)
        self.assertEqual(payload["status"], "compile_failed")
        self.assertIn("must be one of", payload["errors"][0]["error"])

    def test_limit_default_and_max_bound(self) -> None:
        default_result = self.tool.execute(
            {
                "dataset_id": "marketing",
                "metric_names": ["clicks_total"],
            }
        )
        self.assertFalse(default_result.is_error)
        default_payload = json.loads(default_result.content)
        self.assertEqual(default_payload["plans"][0]["limit"], 100)

        max_bound_result = self.tool.execute(
            {
                "dataset_id": "marketing",
                "metric_names": ["clicks_total"],
                "limit": 1001,
            }
        )
        self.assertTrue(max_bound_result.is_error)
        max_payload = json.loads(max_bound_result.content)
        self.assertEqual(max_payload["status"], "compile_failed")
        self.assertIn("between 1 and 1000", max_payload["errors"][0]["error"])

    def _write_source_config(self) -> None:
        source_path = self.metrics_root / "sources" / "ad_performance.yml"
        source_path.write_text(
            """kind: source
version: 1
id: ad_performance
dataset: marketing
table: campaign_ads
grain:
  - campaign_ad_id
dimensions:
  - name: campaign_ad_id
    expr: campaign_ad_id
    data_type: INT64
    is_primary_key: true
  - name: campaign_id
    expr: campaign_id
    data_type: INT64
  - name: start_date
    expr: start_date
    data_type: DATE
measures:
  - name: spend_total
    expr: spend
    agg: SUM
    data_type: NUMERIC
  - name: click_count
    expr: clicks
    agg: SUM
    data_type: INT64
  - name: impression_count
    expr: impressions
    agg: SUM
    data_type: INT64
""",
            encoding="utf-8",
        )

    def _write_metric_configs(self) -> None:
        metrics = {
            "spend_total": """kind: metric
version: 1
id: spend_total
label: Total Spend
type: simple
base_source: ad_performance
expr: spend
dimensions:
  - campaign_id
format: \"$#,##0.00\"
""",
            "clicks_total": """kind: metric
version: 1
id: clicks_total
label: Total Clicks
type: simple
base_source: ad_performance
expr: clicks
dimensions:
  - campaign_id
format: \"#,##0\"
""",
            "impressions_total": """kind: metric
version: 1
id: impressions_total
label: Total Impressions
type: simple
base_source: ad_performance
expr: impressions
dimensions:
  - campaign_id
format: \"#,##0\"
""",
            "ctr": """kind: metric
version: 1
id: ctr
label: Click Through Rate
type: ratio
base_source: ad_performance
numerator: clicks_total
denominator: impressions_total
dimensions:
  - campaign_id
format: \"0.00%\"
""",
            "cpm": """kind: metric
version: 1
id: cpm
label: Cost Per Mille
type: ratio
base_source: ad_performance
numerator: spend_total
denominator: impressions_total / 1000
dimensions:
  - campaign_id
format: \"$#,##0.00\"
""",
            "broken_source": """kind: metric
version: 1
id: broken_source
label: Broken Source Metric
type: simple
base_source: missing_source
expr: spend
""",
        }

        for metric_name, content in metrics.items():
            metric_path = self.metrics_root / "metrics" / f"{metric_name}.yml"
            metric_path.write_text(content, encoding="utf-8")


class LocalToolsStructureTests(unittest.TestCase):
    def test_local_tools_has_only_build_functions(self) -> None:
        local_tools_path = Path(__file__).resolve().parent / "local_tools.py"
        module = ast.parse(local_tools_path.read_text(encoding="utf-8"))
        function_names = [
            node.name for node in module.body if isinstance(node, ast.FunctionDef)
        ]
        self.assertEqual(
            function_names,
            ["build_steward_tools", "build_analyst_tools"],
        )


if __name__ == "__main__":
    unittest.main()
