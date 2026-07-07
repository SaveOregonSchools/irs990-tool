import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import grant_ai_assist_v1 as gai
import grant_ai_batch_worker as worker


def worker_args(output_dir: str) -> SimpleNamespace:
    return SimpleNamespace(
        input_dir="/data/packets",
        input_file=None,
        pattern="adjudication_packets_*.jsonl",
        output_dir=output_dir,
        decision_prefix="decisions",
        model="grant-ai-adjudicator:gemma4-12b",
        format_mode="schema",
        think=False,
        num_ctx=8192,
        num_predict=700,
        temperature=0.0,
        keep_alive="30m",
        timeout=180,
        error_retries=2,
        retry_backoff_seconds=2.0,
        retry_backoff_multiplier=1.5,
        candidate_id_retries=1,
        max_explanation_words=35,
        system_prompt_file="",
        omit_system_message=False,
        write_failures_as_human_review=False,
        parallel_workers=4,
        run_manifest="worker_run_manifest.json",
        overwrite=False,
    )


class GrantAiBatchManifestTests(unittest.TestCase):
    def test_worker_writes_run_manifest_and_slims_per_record_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = worker_args(tmp)
            manifest_path = worker.write_run_manifest(args, [Path("adjudication_packets_000001.jsonl")])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

            self.assertEqual(manifest["format"], "grant_ai_worker_run_manifest_v1")
            self.assertEqual(manifest["settings"]["model"], "grant-ai-adjudicator:gemma4-12b")
            self.assertEqual(manifest["settings"]["num_predict"], 700)
            self.assertEqual(manifest["settings"]["max_explanation_words"], 35)

            original = worker.call_with_candidate_id_retries
            try:
                worker.call_with_candidate_id_retries = lambda packet, _args: (
                    {
                        "decision": "NO_MATCH",
                        "candidate_id": "",
                        "confidence": 1.0,
                        "confidence_label": "high",
                        "reason_codes": ["name_mismatch"],
                        "explanation": "Names do not match.",
                        "needs_human_review": False,
                    },
                    0,
                )
                sig, rec, err = worker.process_one({"signature_hash": "SIG_TEST", "input": {}}, args)
            finally:
                worker.call_with_candidate_id_retries = original

            self.assertEqual(sig, "SIG_TEST")
            self.assertIsNone(err)
            self.assertNotIn("model", rec["worker"])
            self.assertNotIn("format_mode", rec["worker"])
            self.assertNotIn("num_predict", rec["worker"])
            self.assertNotIn("think", rec["worker"])
            self.assertIn("processed_at", rec["worker"])
            self.assertEqual(rec["worker"]["candidate_id_retries_used"], 0)

    def test_import_row_args_uses_manifest_when_source_model_is_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "worker_run_manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "format": "grant_ai_worker_run_manifest_v1",
                        "settings": {
                            "model": "grant-ai-adjudicator:gemma4-12b",
                            "format_mode": "schema",
                            "think": False,
                            "num_ctx": 8192,
                            "num_predict": 700,
                            "temperature": 0.0,
                            "top_p": 0.9,
                        },
                    }
                ),
                encoding="utf-8",
            )
            decision_file = Path(tmp) / "decisions_000001.jsonl"
            decision_file.write_text("", encoding="utf-8")

            manifest = gai.load_external_decision_manifest(decision_file)
            row_args = gai.external_import_row_args(
                SimpleNamespace(source_model="external:linux-ollama"),
                manifest,
            )

            self.assertEqual(row_args.model, "grant-ai-adjudicator:gemma4-12b")
            self.assertEqual(row_args.num_ctx, 8192)
            self.assertEqual(row_args.num_predict, 700)
            self.assertEqual(row_args.format_mode, "schema")

            overridden = gai.external_import_row_args(
                SimpleNamespace(source_model="manual:model-label"),
                manifest,
            )
            self.assertEqual(overridden.model, "manual:model-label")


if __name__ == "__main__":
    unittest.main()
