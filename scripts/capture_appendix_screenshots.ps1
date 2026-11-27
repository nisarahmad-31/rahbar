<#
.SYNOPSIS
Captures uncropped full-screen Windows Terminal screenshots for Assignment 03 Appendix (Parts A-F).
Run directly inside your interactive PowerShell / Windows Terminal on Windows desktop:
    powershell -ExecutionPolicy Bypass -File .\scripts\capture_appendix_screenshots.ps1
#>

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$OutDir = Join-Path $RepoRoot "docs\assignment-03\screenshots"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class ScreenCapUtil {
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
}
'@
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[ScreenCapUtil]::SetProcessDPIAware() | Out-Null

# Maximize current Windows Terminal (if hosted inside wt) or Console window
$wt = Get-Process WindowsTerminal -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
if ($wt) {
    [ScreenCapUtil]::ShowWindow($wt.MainWindowHandle, 3) | Out-Null
    [ScreenCapUtil]::SetForegroundWindow($wt.MainWindowHandle) | Out-Null
} else {
    $con = [ScreenCapUtil]::GetConsoleWindow()
    if ($con -ne [IntPtr]::Zero) {
        [ScreenCapUtil]::ShowWindow($con, 3) | Out-Null
        [ScreenCapUtil]::SetForegroundWindow($con) | Out-Null
    }
}
Start-Sleep -Milliseconds 700

$Shots = @(
    @{
        Name = "A1_verification_batch_b.png"
        Cmd  = @'
clear
cd "/mnt/e/S5/SCD/#3/rahbar"
echo -e "\033[1;32mmussie23@rahbar\033[0m:\033[1;34m~/rahbar\033[0m$ PYTHONPATH=indexer .venv/bin/python -m pipeline.run --corpus-dir corpus/batches/batch_b --batch-name batch-b --verify-only --report-out /tmp/shot_batch_b.json"
PYTHONPATH=indexer .venv/bin/python -m pipeline.run --corpus-dir corpus/batches/batch_b --batch-name batch-b --verify-only --report-out /tmp/shot_batch_b.json
echo -e "\033[1;32mmussie23@rahbar\033[0m:\033[1;34m~/rahbar\033[0m$ # Per-document verification results (eval/results/verification_batch_b.json)"
.venv/bin/python - <<'PY'
import json
d = json.load(open("eval/results/verification_batch_b.json"))
print("Summary:", d["summary"])
for x in d["documents"]:
    fails = [f"{k}={v['reason']}" for k, v in x["checks"].items() if v["status"] != "pass"]
    checks = " ".join(f"{k}:{v['status']}" for k, v in x["checks"].items())
    print(f"  {x['source']:36s} | {x['status']:4s} | quar={str(x['quarantined']):5s} | {checks}" + (f" | {fails[0][:48]}" if fails else ""))
PY
'@
    },
    @{
        Name = "A2_unchanged_zero_reembed.png"
        Cmd  = @'
clear
cd "/mnt/e/S5/SCD/#3/rahbar"
echo -e "\033[1;32mmussie23@rahbar\033[0m:\033[1;34m~/rahbar\033[0m$ kubectl logs job/reindex-unchanged -n rahbar"
kubectl logs job/reindex-unchanged -n rahbar
echo -e "\033[1;32mmussie23@rahbar\033[0m:\033[1;34m~/rahbar\033[0m$ # A2 incremental evidence (eval/results/a2_incremental_evidence.json)"
.venv/bin/python - <<'PY'
import json
d = json.load(open("eval/results/a2_incremental_evidence.json"))
for k in ("step1_batch_a_initial", "step2_unchanged_rerun", "step3_modified_one_doc", "step4_deleted_one_doc", "step5_batch_b_merge_superseded", "step6_restore_2024_version"):
    if k in d:
        s = d[k]
        print(f"{k:32s} -> embedded={s.get('chunks_embedded','-')}, calls={s.get('embed_api_calls','-')}, total={s.get('total_chunks_in_store','-')}, extra={{k2:s[k2] for k2 in ('documents_unchanged','changed_doc_ids','deleted_doc_ids','superseded_versions','active_has_2024','demoted_version') if k2 in s}}")
PY
'@
    },
    @{
        Name = "B_three_level_eval.png"
        Cmd  = @'
clear
cd "/mnt/e/S5/SCD/#3/rahbar"
echo -e "\033[1;32mmussie23@rahbar\033[0m:\033[1;34m~/rahbar\033[0m$ # Part B 3-Level Evaluation Summary (eval/results/part_b_baseline_latest.json)"
.venv/bin/python - <<'PY'
import json
d = json.load(open("eval/results/part_b_baseline_latest.json"))
m = d["module_embedding"]
print(f"Model: {m['model']} (dim={m['dimensions']}, chunk_size={m['chunk_size']}, seed={d['seed']}, determinism max_abs_diff={m['determinism']['max_abs_diff']})")
print()
print(d["summary_table_markdown"])
print()
ag = d.get("judge_human_agreement") or {}
print(f"LLM Judge (llama3.2:3b) vs Human Agreement: {ag.get('agreed_count')}/{ag.get('labeled_count')} ({ag.get('agreement_rate',0)*100:.2f}%)")
PY
'@
    },
    @{
        Name = "C_release_gate_fail_pass.png"
        Cmd  = @'
clear
cd "/mnt/e/S5/SCD/#3/rahbar"
echo -e "\033[1;32mmussie23@rahbar\033[0m:\033[1;34m~/rahbar\033[0m$ .venv/bin/python eval/release_gate.py --baseline eval/results/part_b_baseline_latest.json --candidate eval/results/part_b_candidate_topk1_latest.json --config eval/gate_config.json"
.venv/bin/python eval/release_gate.py --baseline eval/results/part_b_baseline_latest.json --candidate eval/results/part_b_candidate_topk1_latest.json --config eval/gate_config.json || echo "Exit code: $?"
echo ""
echo -e "\033[1;32mmussie23@rahbar\033[0m:\033[1;34m~/rahbar\033[0m$ .venv/bin/python eval/release_gate.py --baseline eval/results/part_b_baseline_latest.json --candidate eval/results/part_b_candidate_topk3_latest.json --config eval/gate_config.json"
.venv/bin/python eval/release_gate.py --baseline eval/results/part_b_baseline_latest.json --candidate eval/results/part_b_candidate_topk3_latest.json --config eval/gate_config.json
echo "Exit code: $?"
'@
    },
    @{
        Name = "D1_full_trace_record.png"
        Cmd  = @'
clear
cd "/mnt/e/S5/SCD/#3/rahbar"
echo -e "\033[1;32mmussie23@rahbar\033[0m:\033[1;34m~/rahbar\033[0m$ # Full D1 TraceRecord + D4 Guardrail Neutralization Record"
.venv/bin/python - <<'PY'
import json
rec = json.loads(open("traces/traces.jsonl").read().splitlines()[0])
compact = dict(rec)
compact["prompt"] = compact["prompt"][:160] + "... [full prompt in inspect_trace]"
compact["retrieved_chunks"] = [{"chunk_id": c["chunk_id"], "doc_id": c["doc_id"], "doc_version": c["doc_version"], "score": c["score"], "text": c["text"][:60]+"..."} for c in rec["retrieved_chunks"]]
print(json.dumps(compact, indent=2)[:1550])
print("--- D4 Live Guardrail Trace (b11_injected_advisor_notice.md) ---")
for line in open("traces/e3_batch_b_traces.jsonl"):
    r = json.loads(line)
    if any("retrieval_injection" in a for a in r.get("guardrail_actions", [])):
        print(json.dumps({"trace_id": r["trace_id"], "status": r["status"], "guardrail_actions": r["guardrail_actions"], "response": r["response"][:100]}))
        break
PY
'@
    },
    @{
        Name = "D2_trace_inspector_prompt.png"
        Cmd  = @'
clear
cd "/mnt/e/S5/SCD/#3/rahbar"
echo -e "\033[1;32mmussie23@rahbar\033[0m:\033[1;34m~/rahbar\033[0m$ .venv/bin/python scripts/inspect_trace.py 87a7cca0db8b44a6acc3661e56cb5a8f --trace-file traces/traces.jsonl"
.venv/bin/python scripts/inspect_trace.py 87a7cca0db8b44a6acc3661e56cb5a8f --trace-file traces/traces.jsonl | head -n 40
'@
    },
    @{
        Name = "E_coverage_before_after.png"
        Cmd  = @'
clear
cd "/mnt/e/S5/SCD/#3/rahbar"
echo -e "\033[1;32mmussie23@rahbar\033[0m:\033[1;34m~/rahbar\033[0m$ # Part E Coverage Before (28 golden) vs After (42 golden, +14 from uncovered topics)"
.venv/bin/python - <<'PY'
import json
for label, path in [("BEFORE EXPANSION (E2)", "eval/results/coverage_e2_before.json"), ("AFTER EXPANSION (E4)", "eval/results/coverage_e4_after.json")]:
    d = json.load(open(path))
    rc = 0 if d["coverage_rate"] >= d["min_coverage_rate"] else 1
    print(f"=== {label} ===")
    print(f"Live={d['total_live_queries']} | Golden={d['total_golden_questions']} | CosineThreshold={d['threshold']} | Covered={d['covered_count']}/{d['total_live_queries']} ({d['coverage_rate']}) | Min={d['min_coverage_rate']} | Exit={rc}")
    topics = ", ".join(f"{k}({len(v)})" for k, v in sorted(d["uncovered_topic_groups"].items()))
    print("Uncovered topics:", topics)
    print()
PY
echo -e "\033[1;32mmussie23@rahbar\033[0m:\033[1;34m~/rahbar\033[0m$ .venv/bin/python eval/query_coverage.py --threshold 0.72 --min-coverage 0.75 --out /tmp/shot_cov.json"
.venv/bin/python eval/query_coverage.py --threshold 0.72 --min-coverage 0.75 --out /tmp/shot_cov.json
'@
    },
    @{
        Name = "F1_k8s_resources.png"
        Cmd  = @'
clear
cd "/mnt/e/S5/SCD/#3/rahbar"
echo -e "\033[1;32mmussie23@rahbar\033[0m:\033[1;34m~/rahbar\033[0m$ kubectl get pods,svc,deploy,sts,cronjob,job -n rahbar"
kubectl get pods,svc,deploy,sts,cronjob,job -n rahbar
echo ""
echo -e "\033[1;32mmussie23@rahbar\033[0m:\033[1;34m~/rahbar\033[0m$ kubectl get pvc,cm -n rahbar"
kubectl get pvc,cm -n rahbar
'@
    },
    @{
        Name = "F2_k8s_jobs_and_configmap_flip.png"
        Cmd  = @'
clear
cd "/mnt/e/S5/SCD/#3/rahbar"
echo -e "\033[1;32mmussie23@rahbar\033[0m:\033[1;34m~/rahbar\033[0m$ kubectl logs job/indexer-batch-b -n rahbar"
kubectl logs job/indexer-batch-b -n rahbar
echo -e "\033[1;32mmussie23@rahbar\033[0m:\033[1;34m~/rahbar\033[0m$ kubectl logs job/eval-gate -n rahbar"
kubectl logs job/eval-gate -n rahbar
echo -e "\033[1;32mmussie23@rahbar\033[0m:\033[1;34m~/rahbar\033[0m$ # ConfigMap Gate Flip WITHOUT Image Rebuild (PASS -> FAIL)"
.venv/bin/python - <<'PY'
import json
for lbl, f in [("k8s_candidate_latest (default ConfigMap)", "eval/results/k8s/gate_k8s_candidate_latest.json"), ("k8s_configmap_flip_latest (patched ConfigMap)", "eval/results/k8s/gate_k8s_configmap_flip_latest.json")]:
    g = json.load(open(f))
    fails = [f"{c['metric']}={c['measured']}({c['threshold']})" for c in g["checks"] if not c["passed"]]
    print(f"  {lbl}: decision={g['decision']} | failed_checks={fails}")
PY
'@
    },
    @{
        Name = "F3_k8s_rollout_undo_trace.png"
        Cmd  = @'
clear
cd "/mnt/e/S5/SCD/#3/rahbar"
echo -e "\033[1;32mmussie23@rahbar\033[0m:\033[1;34m~/rahbar\033[0m$ kubectl rollout history deployment/api -n rahbar && kubectl rollout status deployment/api -n rahbar"
kubectl rollout history deployment/api -n rahbar
kubectl rollout status deployment/api -n rahbar
echo -e "\033[1;32mmussie23@rahbar\033[0m:\033[1;34m~/rahbar\033[0m$ # Post-Rollback Trace from kubectl logs (traces/k8s_post_rollback_trace.jsonl)"
.venv/bin/python - <<'PY'
import json
r = json.loads(open("traces/k8s_post_rollback_trace.jsonl").read().splitlines()[0])
print(json.dumps({
    "trace_id": r["trace_id"],
    "timestamp": r["timestamp"],
    "embedding_model": r["embedding_model"],
    "embedding_version": r["embedding_version"],
    "collection_name": r["collection_name"],
    "collection_version": r["collection_version"],
    "status": r["status"],
    "top_chunk": r["retrieved_chunks"][0]["chunk_id"],
    "response": r["response"][:120],
}, indent=2))
PY
'@
    }
)

foreach ($shot in $Shots) {
    $b64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes(($shot.Cmd -replace "`r","")))
    wsl -d Ubuntu-24.04 -- bash -c "echo $b64 | base64 -d | bash"
    Start-Sleep -Milliseconds 1200

    $bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
    $bmp = New-Object System.Drawing.Bitmap($bounds.Width, $bounds.Height)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
    $dest = Join-Path $OutDir $shot.Name
    $bmp.Save($dest, [System.Drawing.Imaging.ImageFormat]::Png)
    $g.Dispose(); $bmp.Dispose()
}

clear
Write-Host "Captured $($Shots.Count) screenshots in: $OutDir" -ForegroundColor Green
Get-ChildItem $OutDir | Format-Table Name, Length
