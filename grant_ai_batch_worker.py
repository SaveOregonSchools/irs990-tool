#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
grant_ai_batch_worker.py

Standalone Linux/Ollama worker for grant-recipient adjudication packets exported
by grant_ai_assist_v1_26_batch_adjudication.py.

This script DOES NOT need the IRS 990 SQLite database. It reads JSONL packet files
created on the Windows/database machine, sends each packet to a local or remote
Ollama /api/chat endpoint, and writes importable decision JSONL files that can be
copied back to Windows and imported with:

    python grant_ai_assist_v1.py import-adjudication-decision-dir --in-dir <decision_dir> --dry-run

Key features:
- Resumable per input file. Existing output decisions are read and skipped unless
  --overwrite is used.
- Writes one decision file per input packet file, for example:
      adjudication_packets_000001.jsonl -> decisions_000001.jsonl
- Writes separate error files so failed model calls can be retried later.
- Supports client-side parallel requests with --parallel-workers. Actual speedup
  depends on Ollama server/model support for parallel generation.
- Disables thinking by default to avoid models returning only hidden reasoning
  and empty message.content.
- v1.27 adds --error-retries / --retry-backoff-seconds for transient
  Ollama/JSON failures, --candidate-id-retries for cases where the model
  tries to select a candidate but omits/garbles candidate_id, stricter
  candidate_id prompt/schema language, and optional --system-prompt-file /
  --omit-system-message support for custom Ollama Modelfile workflows.

Typical Linux use:

    python3 grant_ai_batch_worker.py \
      --input-dir /data/irs990_ai/packets \
      --output-dir /data/irs990_ai/decisions \
      --model gemma4:12b \
      --ollama-url http://127.0.0.1:11434/api/chat \
      --parallel-workers 2

The output files are importable by the Windows-side grant_ai_assist script.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

AI_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["SELECT_CANDIDATE", "KEEP_REPORTED_EIN", "NO_MATCH", "AMBIGUOUS", "HUMAN_REVIEW"]},
        "candidate_id": {"type": "string", "description": "Required when decision is SELECT_CANDIDATE. Must match one of the provided candidate_id values exactly."},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1, "description": "Decimal confidence between 0 and 1, not a percent. Use 0.95, not 95 or 100."},
        "confidence_label": {"type": "string", "enum": ["high", "medium", "low", "none"]},
        "reason_codes": {"type": "array", "items": {"type": "string"}},
        "explanation": {"type": "string"},
        "needs_human_review": {"type": "boolean"},
    },
    "required": ["decision", "candidate_id", "confidence", "confidence_label", "reason_codes", "explanation", "needs_human_review"],
    "additionalProperties": False,
}

ALLOWED_DECISIONS = {"SELECT_CANDIDATE", "KEEP_REPORTED_EIN", "NO_MATCH", "AMBIGUOUS", "HUMAN_REVIEW"}


def now_stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def clean_text(x: Any) -> str:
    if x is None:
        return ""
    return str(x).strip()


def read_jsonl(path: Path) -> Iterator[Dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise RuntimeError(f"Invalid JSON at {path}:{line_no}: {e}") from e
            if not isinstance(obj, dict):
                raise RuntimeError(f"Non-object JSONL record at {path}:{line_no}")
            yield obj


def packet_signature(packet: Dict[str, Any]) -> str:
    return clean_text(packet.get("signature_hash") or (packet.get("input") or {}).get("grant_recipient_signature", {}).get("signature_hash"))


def packet_candidates(packet: Dict[str, Any]) -> List[Dict[str, Any]]:
    inp = packet.get("input") or {}
    cands = inp.get("candidates") or []
    return cands if isinstance(cands, list) else []


def decision_output_path(input_path: Path, output_dir: Path, decision_prefix: str) -> Path:
    stem = input_path.stem
    m = re.search(r"(\d{6,})$", stem)
    if m:
        suffix = m.group(1)
        return output_dir / f"{decision_prefix}_{suffix}.jsonl"
    return output_dir / f"{decision_prefix}_{stem}.jsonl"


def error_output_path(input_path: Path, output_dir: Path) -> Path:
    stem = input_path.stem
    m = re.search(r"(\d{6,})$", stem)
    if m:
        suffix = m.group(1)
        return output_dir / f"errors_{suffix}.jsonl"
    return output_dir / f"errors_{stem}.jsonl"


def existing_decision_signatures(path: Path) -> set[str]:
    seen: set[str] = set()
    if not path.exists():
        return seen
    try:
        for obj in read_jsonl(path):
            sig = clean_text(obj.get("signature_hash") or (obj.get("output") or {}).get("signature_hash"))
            if sig:
                seen.add(sig)
    except Exception:
        # If the existing output is corrupt, fail loudly by returning a sentinel
        # impossible to silently skip everything.
        raise
    return seen


def clean_model_content(text: str) -> str:
    s = (text or "").strip()
    if s.startswith("```"):
        lines = s.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    if s.lower().startswith("json\n"):
        s = s[5:].strip()
    # Best effort if the model wraps JSON in explanatory text.
    if not s.startswith("{"):
        m = re.search(r"\{.*\}", s, flags=re.DOTALL)
        if m:
            s = m.group(0)
    return s


def label_for_confidence(conf: float) -> str:
    if conf >= 0.9:
        return "high"
    if conf >= 0.7:
        return "medium"
    if conf > 0:
        return "low"
    return "none"


def normalize_model_output(output: Dict[str, Any], packet: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize and lightly repair model output before Windows import.

    Windows-side import remains the source of truth. This just reduces avoidable
    invalid rows by fixing percent confidence, missing candidate_id when obvious,
    and invalid decision names.
    """
    out = dict(output or {})
    if "selected_candidate_id" in out and "candidate_id" not in out:
        out["candidate_id"] = out.get("selected_candidate_id")
    decision = clean_text(out.get("decision")).upper()
    if decision not in ALLOWED_DECISIONS:
        decision = "HUMAN_REVIEW"
        out["reason_codes"] = ["worker_invalid_decision"]
        out["explanation"] = clean_text(out.get("explanation")) or "Worker changed invalid/missing model decision to HUMAN_REVIEW."
        out["needs_human_review"] = True
    out["decision"] = decision

    try:
        conf = float(out.get("confidence"))
    except Exception:
        conf = 0.0 if decision in {"NO_MATCH", "HUMAN_REVIEW", "AMBIGUOUS"} else 0.5
    if conf > 1 and conf <= 100:
        conf = conf / 100.0
    if conf < 0 or conf > 1:
        conf = 0.0
    out["confidence"] = round(conf, 4)
    if not clean_text(out.get("confidence_label")):
        out["confidence_label"] = label_for_confidence(conf)

    rc = out.get("reason_codes")
    if isinstance(rc, str):
        try:
            rc2 = json.loads(rc)
            rc = rc2 if isinstance(rc2, list) else [rc]
        except Exception:
            rc = [r.strip() for r in re.split(r"[,;|]", rc) if r.strip()]
    if not isinstance(rc, list):
        rc = []
    out["reason_codes"] = [clean_text(r) for r in rc if clean_text(r)]

    if "needs_human_review" not in out:
        out["needs_human_review"] = decision in {"HUMAN_REVIEW", "AMBIGUOUS"} or conf < 0.9
    else:
        val = out.get("needs_human_review")
        if isinstance(val, str):
            out["needs_human_review"] = val.strip().lower() in {"1", "true", "yes", "y", "on"}
        else:
            out["needs_human_review"] = bool(val)

    if "explanation" not in out:
        out["explanation"] = ""
    out["explanation"] = clean_text(out.get("explanation"))

    candidate_ids = {clean_text(c.get("candidate_id")) for c in packet_candidates(packet)}
    candidate_ids.discard("")
    cand_id = clean_text(out.get("candidate_id"))
    if decision == "SELECT_CANDIDATE":
        if not cand_id:
            # Recover only when unambiguous.
            text = " ".join([out.get("explanation", ""), " ".join(out.get("reason_codes", []))])
            found = sorted(set(re.findall(r"\bC\d+\b", text)))
            if len(found) == 1 and found[0] in candidate_ids:
                cand_id = found[0]
            elif len(candidate_ids) == 1:
                cand_id = next(iter(candidate_ids))
        if cand_id not in candidate_ids:
            out["decision"] = "HUMAN_REVIEW"
            out["candidate_id"] = ""
            out["confidence"] = min(float(out.get("confidence") or 0.5), 0.75)
            out["confidence_label"] = label_for_confidence(out["confidence"])
            out["reason_codes"] = list(out.get("reason_codes") or []) + ["worker_missing_or_invalid_candidate_id"]
            out["explanation"] = (out.get("explanation") or "") + " Worker changed SELECT_CANDIDATE to HUMAN_REVIEW because candidate_id was missing or invalid."
            out["needs_human_review"] = True
        else:
            out["candidate_id"] = cand_id
    else:
        out["candidate_id"] = clean_text(out.get("candidate_id")) if clean_text(out.get("candidate_id")) in candidate_ids else ""

    return out


class CandidateIdRetryNeeded(RuntimeError):
    pass


def default_system_prompt() -> str:
    return """
You are a careful nonprofit identity matching adjudicator.
You receive one grant-recipient record and a candidate list generated by a database.
Your job is to choose the correct candidate when the evidence is strong, not to require absolute certainty.
Return only JSON that follows the provided schema.

CRITICAL OUTPUT RULES:
- Every response MUST include candidate_id.
- If decision is SELECT_CANDIDATE, candidate_id is REQUIRED and must exactly equal one of the provided candidate_id values such as C1, C2, etc.
- If decision is NO_MATCH, AMBIGUOUS, HUMAN_REVIEW, or KEEP_REPORTED_EIN, candidate_id must be an empty string unless a candidate_id is explicitly relevant.
- If you cannot identify the exact candidate_id to select, do NOT use SELECT_CANDIDATE; use HUMAN_REVIEW or AMBIGUOUS.
- Confidence must be a decimal between 0 and 1, for example 0.95. Never return 95 or 100.
- Never invent an EIN or candidate ID.

MATCHING RULES:
- A blank reported EIN is common in grant schedules and is not by itself a reason for ambiguity.
- Legal suffix/noise differences such as INC, FOUNDATION, FUND, THE, LLC, CORP, CO, LTD, ASSOCIATION, punctuation, and spacing are weak evidence and should not block a match when core name plus address/location agree.
- If one candidate has exact name/address/ZIP evidence and no similarly strong alternative, return SELECT_CANDIDATE with high confidence and needs_human_review=false.
- Return AMBIGUOUS or HUMAN_REVIEW when evidence is genuinely conflicting, weak, or there are multiple similarly plausible candidates.
- The fields sample_grantor_name and sample_grantor_ein identify the funder/filer/grantor, NOT the recipient. Do not use a match to sample_grantor_name as evidence that a candidate is the recipient.
- If the recipient name is a placeholder or non-organization label such as Eligible Patients, Various Recipients, Individuals, Scholarship Recipients, or See Schedule/Statement, do not resolve it to the grantor; return NO_MATCH or HUMAN_REVIEW unless there is separate recipient evidence.
- Precision is more important than recall: a wrong EIN is worse than no match, but do not overuse HUMAN_REVIEW for obvious exact matches.

FINAL CHECK BEFORE RESPONDING:
- If decision=SELECT_CANDIDATE, copy the exact candidate_id from the candidate list into candidate_id.
- If candidate_id is missing or invalid, the worker will reject your answer and retry or mark it HUMAN_REVIEW.
""".strip()


def get_system_prompt(args: argparse.Namespace) -> str:
    if getattr(args, "omit_system_message", False):
        return ""
    path = clean_text(getattr(args, "system_prompt_file", ""))
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    return default_system_prompt()


def candidate_id_list_text(packet: Dict[str, Any]) -> str:
    parts: List[str] = []
    for c in packet_candidates(packet):
        cid = clean_text(c.get("candidate_id"))
        ein = clean_text(c.get("ein") or c.get("candidate_ein"))
        name = clean_text(c.get("candidate_name") or c.get("name"))
        if cid:
            parts.append(f"{cid} = EIN {ein}, name {name}")
    return "; ".join(parts)


def build_messages(packet: Dict[str, Any], args: argparse.Namespace, extra_user_instruction: str = "") -> List[Dict[str, str]]:
    input_obj = packet.get("input") or {}
    messages: List[Dict[str, str]] = []
    system_msg = get_system_prompt(args)
    if system_msg:
        messages.append({"role": "system", "content": system_msg})
    user_content = json.dumps(input_obj, ensure_ascii=False, sort_keys=True)
    if extra_user_instruction:
        user_content = (
            user_content
            + "\n\nADDITIONAL WORKER INSTRUCTION:\n"
            + extra_user_instruction.strip()
        )
    messages.append({"role": "user", "content": user_content})
    return messages


def call_ollama(packet: Dict[str, Any], args: argparse.Namespace, extra_user_instruction: str = "") -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": args.model,
        "messages": build_messages(packet, args, extra_user_instruction=extra_user_instruction),
        "stream": False,
        "keep_alive": args.keep_alive,
        "think": bool(args.think),
        "options": {
            "temperature": args.temperature,
            "top_p": 0.9,
            "num_ctx": args.num_ctx,
            "num_predict": args.num_predict,
        },
    }
    if args.format_mode == "schema":
        payload["format"] = AI_DECISION_SCHEMA
    elif args.format_mode == "json":
        payload["format"] = "json"
    elif args.format_mode == "none":
        pass
    else:
        raise ValueError(f"Unknown format mode: {args.format_mode}")

    req = urllib.request.Request(
        args.ollama_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=args.timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    data = json.loads(raw)
    msg = data.get("message") or {}
    content = msg.get("content") or data.get("response") or ""
    if not content:
        diag = {k: data.get(k) for k in ("done", "done_reason", "model", "eval_count", "prompt_eval_count") if k in data}
        raise RuntimeError(f"Ollama returned empty message.content; diagnostics={diag}")
    cleaned = clean_model_content(content)
    output = json.loads(cleaned)
    if not isinstance(output, dict):
        raise RuntimeError("Ollama content parsed but was not a JSON object")
    return normalize_model_output(output, packet)


def should_retry_candidate_id(output: Dict[str, Any]) -> bool:
    if clean_text(output.get("decision")) != "HUMAN_REVIEW":
        return False
    return "worker_missing_or_invalid_candidate_id" in set(output.get("reason_codes") or [])


def call_with_candidate_id_retries(packet: Dict[str, Any], args: argparse.Namespace) -> Tuple[Dict[str, Any], int]:
    output = call_ollama(packet, args)
    retries_used = 0
    while should_retry_candidate_id(output) and retries_used < max(0, int(args.candidate_id_retries or 0)):
        retries_used += 1
        extra = f"""
Your previous answer tried to SELECT_CANDIDATE but did not provide a valid candidate_id.
Candidate IDs available for this packet are: {candidate_id_list_text(packet)}.
Return JSON only. If you choose SELECT_CANDIDATE, candidate_id MUST be exactly one of those IDs.
If none is strong enough or you are unsure, return HUMAN_REVIEW or AMBIGUOUS with candidate_id as an empty string.
""".strip()
        try:
            retry_output = call_ollama(packet, args, extra_user_instruction=extra)
            output = retry_output
        except Exception:
            # Leave the original safe HUMAN_REVIEW output in place if repair retry fails.
            break
    return output, retries_used


def process_one(packet: Dict[str, Any], args: argparse.Namespace) -> Tuple[str, Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    sig = packet_signature(packet)
    attempt_errors: List[str] = []
    max_attempts = 1 + max(0, int(args.error_retries or 0))
    for attempt in range(1, max_attempts + 1):
        try:
            output, cid_retries = call_with_candidate_id_retries(packet, args)
            rec = {
                "signature_hash": sig,
                "output": output,
                "worker": {
                    "processed_at": now_stamp(),
                    "model": args.model,
                    "format_mode": args.format_mode,
                    "think": bool(args.think),
                    "num_predict": args.num_predict,
                    "attempt": attempt,
                    "error_retries_configured": int(args.error_retries or 0),
                    "candidate_id_retries_used": cid_retries,
                },
            }
            return sig, rec, None
        except Exception as e:
            attempt_errors.append(f"attempt {attempt}/{max_attempts}: {type(e).__name__}: {e}")
            if attempt < max_attempts:
                delay = float(args.retry_backoff_seconds or 0) * (float(args.retry_backoff_multiplier or 1.0) ** (attempt - 1))
                if delay > 0:
                    time.sleep(delay)
                continue
            err = {
                "signature_hash": sig,
                "error": attempt_errors[-1],
                "attempt_errors": attempt_errors,
                "attempts": attempt,
                "failed_at": now_stamp(),
                "model": args.model,
            }
            if args.write_failures_as_human_review:
                rec = {
                    "signature_hash": sig,
                    "output": {
                        "decision": "HUMAN_REVIEW",
                        "candidate_id": "",
                        "confidence": 0.0,
                        "confidence_label": "none",
                        "reason_codes": ["worker_ollama_call_failed"],
                        "explanation": err["error"],
                        "needs_human_review": True,
                    },
                    "worker": {
                        "processed_at": now_stamp(),
                        "model": args.model,
                        "failure_recorded_as_human_review": True,
                        "attempts": attempt,
                        "attempt_errors": attempt_errors,
                    },
                }
                return sig, rec, err
            return sig, None, err

def iter_packet_files(args: argparse.Namespace) -> List[Path]:
    if args.input_file:
        return [Path(args.input_file)]
    in_dir = Path(args.input_dir)
    return sorted(p for p in in_dir.glob(args.pattern) if p.is_file())


def process_file(input_path: Path, args: argparse.Namespace) -> Tuple[int, int, int]:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = decision_output_path(input_path, output_dir, args.decision_prefix)
    err_path = error_output_path(input_path, output_dir)

    if args.overwrite:
        out_path.unlink(missing_ok=True)
        err_path.unlink(missing_ok=True)

    done = existing_decision_signatures(out_path) if args.resume else set()
    packets = []
    for packet in read_jsonl(input_path):
        sig = packet_signature(packet)
        if not sig:
            continue
        if sig in done:
            continue
        packets.append(packet)
        if args.limit and len(packets) >= args.limit:
            break

    total = len(packets)
    if total == 0:
        print(f"{input_path.name}: nothing to do; existing decisions={len(done):,}", flush=True)
        return 0, 0, 0

    print(f"{input_path.name}: processing {total:,} packets -> {out_path.name} with {args.parallel_workers} worker(s)", flush=True)
    started = time.time()
    ok_count = err_count = processed = 0
    with out_path.open("a", encoding="utf-8") as out_fh, err_path.open("a", encoding="utf-8") as err_fh:
        if args.parallel_workers <= 1:
            for packet in packets:
                sig, rec, err = process_one(packet, args)
                processed += 1
                if rec is not None:
                    out_fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
                    ok_count += 1
                if err is not None:
                    err_fh.write(json.dumps(err, ensure_ascii=False, sort_keys=True) + "\n")
                    err_count += 1
                if processed % args.flush_every == 0:
                    out_fh.flush(); err_fh.flush()
                if processed % args.progress_every == 0:
                    elapsed = max(1.0, time.time() - started)
                    print(f"{input_path.name}: {processed:,}/{total:,} processed; decisions={ok_count:,}; errors={err_count:,}; {processed/elapsed:,.2f}/sec", flush=True)
        else:
            with futures.ThreadPoolExecutor(max_workers=args.parallel_workers) as ex:
                future_map = {ex.submit(process_one, packet, args): packet_signature(packet) for packet in packets}
                for fut in futures.as_completed(future_map):
                    processed += 1
                    try:
                        sig, rec, err = fut.result()
                    except Exception as e:
                        sig = future_map.get(fut, "")
                        rec = None
                        err = {"signature_hash": sig, "error": f"worker_future_error:{type(e).__name__}: {e}", "failed_at": now_stamp(), "model": args.model}
                    if rec is not None:
                        out_fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
                        ok_count += 1
                    if err is not None:
                        err_fh.write(json.dumps(err, ensure_ascii=False, sort_keys=True) + "\n")
                        err_count += 1
                    if processed % args.flush_every == 0:
                        out_fh.flush(); err_fh.flush()
                    if processed % args.progress_every == 0:
                        elapsed = max(1.0, time.time() - started)
                        print(f"{input_path.name}: {processed:,}/{total:,} processed; decisions={ok_count:,}; errors={err_count:,}; {processed/elapsed:,.2f}/sec", flush=True)
        out_fh.flush(); err_fh.flush()
    elapsed = max(1.0, time.time() - started)
    print(f"{input_path.name}: complete; decisions={ok_count:,}; errors={err_count:,}; elapsed={elapsed:,.1f}s; rate={processed/elapsed:,.2f}/sec", flush=True)
    return processed, ok_count, err_count


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Process grant AI adjudication packet JSONL files with Ollama and write importable decisions.")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--input-dir", help="Directory containing packet files")
    src.add_argument("--input-file", help="One packet JSONL file")
    p.add_argument("--pattern", default="adjudication_packets_*.jsonl", help="Glob pattern when using --input-dir")
    p.add_argument("--output-dir", required=True, help="Directory to write decisions_*.jsonl and errors_*.jsonl")
    p.add_argument("--decision-prefix", default="decisions")
    p.add_argument("--model", default="gemma4:12b")
    p.add_argument("--ollama-url", default="http://127.0.0.1:11434/api/chat")
    p.add_argument("--format-mode", choices=["schema", "json", "none"], default="schema")
    p.add_argument("--num-ctx", type=int, default=8192)
    p.add_argument("--num-predict", type=int, default=700)
    p.add_argument("--timeout", type=int, default=180)
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--keep-alive", default="30m")
    p.add_argument("--error-retries", type=int, default=2, help="Retry transient Ollama/JSON failures this many times before writing to errors_*.jsonl. Default: 2")
    p.add_argument("--retry-backoff-seconds", type=float, default=2.0, help="Initial sleep between error retries. Default: 2.0")
    p.add_argument("--retry-backoff-multiplier", type=float, default=1.5, help="Multiplier for retry backoff. Default: 1.5")
    p.add_argument("--candidate-id-retries", type=int, default=1, help="Retry once when model tries SELECT_CANDIDATE but omits/garbles candidate_id. Default: 1")
    p.add_argument("--system-prompt-file", default="", help="Optional file containing system prompt. If omitted, built-in grant adjudicator prompt is used.")
    p.add_argument("--omit-system-message", action="store_true", help="Do not send a system role message. Use only with a custom Ollama model/Modelfile that already embeds the system prompt.")
    p.add_argument("--think", action="store_true", help="Enable Ollama thinking. Default is off and recommended for this workflow.")
    p.add_argument("--parallel-workers", type=int, default=1)
    p.add_argument("--limit", type=int, default=None, help="Limit records per input file; mostly for testing")
    p.add_argument("--max-files", type=int, default=None)
    p.add_argument("--resume", action="store_true", default=True, help="Skip signatures already present in the output decision file. Default true.")
    p.add_argument("--no-resume", dest="resume", action="store_false")
    p.add_argument("--overwrite", action="store_true", help="Delete existing output/error files before processing")
    p.add_argument("--write-failures-as-human-review", action="store_true", help="Also write failed calls as HUMAN_REVIEW decisions. Default leaves them in errors only for retry.")
    p.add_argument("--progress-every", type=int, default=25)
    p.add_argument("--flush-every", type=int, default=25)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    files = iter_packet_files(args)
    if args.max_files:
        files = files[: args.max_files]
    if not files:
        print("No packet files found.", file=sys.stderr)
        return 2
    all_processed = all_ok = all_err = 0
    started = time.time()
    for path in files:
        processed, ok, err = process_file(path, args)
        all_processed += processed
        all_ok += ok
        all_err += err
    elapsed = max(1.0, time.time() - started)
    print(f"All files complete: files={len(files):,}; processed={all_processed:,}; decisions={all_ok:,}; errors={all_err:,}; elapsed={elapsed:,.1f}s; overall_rate={all_processed/elapsed:,.2f}/sec", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
