#!/usr/bin/env python3
"""
DeepFake vs VoiceIt API 3.0 — Spoofing Test
Enrolls a user with real voice samples, then attempts verification with deepfake VPPs.
Uses concurrent requests (up to 5 at a time) for faster execution.

Usage:
  python3 deepfake_spoof_test.py              # Run with all available deepfake samples
  python3 deepfake_spoof_test.py --count 1000 # Run 1000 verification attempts (cycles through samples)
  python3 deepfake_spoof_test.py --workers 5  # Set concurrency level (default: 5)
"""

import os
import sys
import json
import time
import glob
import argparse
import requests
import statistics
from datetime import datetime
from itertools import cycle
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

API_BASE = os.environ.get("API_BASE_URL", "https://api.voiceit.io")
API_KEY = os.environ.get("SPOOF_API_KEY", "")
API_TOKEN = os.environ.get("SPOOF_API_TOKEN", "")
AUTH = (API_KEY, API_TOKEN)
HEADERS = {"platformId": "31", "platformVersion": "DEEPFAKE_TEST"}

CONTENT_LANG = "en-US"
PHRASE = "Never forget tomorrow is a new day"

REAL_DIR = os.path.join(os.path.dirname(__file__), "real_samples")
FAKE_DIR = os.path.join(os.path.dirname(__file__), "deepfake_samples")

# Thread-safe counter for progress
print_lock = threading.Lock()

def api(method, endpoint, **kwargs):
    url = f"{API_BASE}{endpoint}"
    resp = requests.request(method, url, auth=AUTH, headers=HEADERS, **kwargs)
    return resp.json()

def create_user():
    data = api("POST", "/users")
    print(f"[CREATE USER] {data['responseCode']} — userId: {data.get('userId', 'N/A')}")
    return data.get("userId")

def delete_user(user_id):
    data = api("DELETE", f"/users/{user_id}")
    print(f"[DELETE USER] {data['responseCode']}")

def enroll_voice(user_id, audio_path):
    fname = os.path.basename(audio_path)
    with open(audio_path, "rb") as f:
        files = {"recording": (fname, f, "audio/wav")}
        form = {"userId": user_id, "contentLanguage": CONTENT_LANG, "phrase": PHRASE}
        data = api("POST", "/enrollments/voice", data=form, files=files)
    status = data.get("responseCode", "UNKNOWN")
    print(f"  [ENROLL] {fname}: {status} — {data.get('message', '')}")
    return data

def verify_voice(user_id, audio_path):
    fname = os.path.basename(audio_path)
    ext = os.path.splitext(fname)[1].lower()
    mime = "audio/wav" if ext == ".wav" else "audio/mpeg"
    with open(audio_path, "rb") as f:
        files = {"recording": (fname, f, mime)}
        form = {"userId": user_id, "contentLanguage": CONTENT_LANG, "phrase": PHRASE}
        data = api("POST", "/verification/voice", data=form, files=files)
    # Flatten extendedVoiceValues to top level for easier access
    ext_vals = data.get("extendedVoiceValues", {})
    if ext_vals:
        data["siv1Confidence"] = ext_vals.get("siv1Confidence")
        data["siv2Confidence"] = ext_vals.get("siv2Confidence")
    return data

def verify_worker(user_id, attempt_num, audio_path, target_count, counters):
    """Worker function for concurrent verification."""
    data = verify_voice(user_id, audio_path)
    fname = os.path.basename(audio_path)
    conf = data.get("confidence", "N/A")
    code = data.get("responseCode", "UNKNOWN")
    passed = code == "SUCC"

    result_entry = {
        "attempt": attempt_num,
        "file": fname,
        "confidence": conf,
        "code": code,
        "passed": passed,
    }
    if "siv1Confidence" in data:
        result_entry["siv1Confidence"] = data["siv1Confidence"]
    if "siv2Confidence" in data:
        result_entry["siv2Confidence"] = data["siv2Confidence"]
    if "textConfidence" in data:
        result_entry["textConfidence"] = data["textConfidence"]

    with print_lock:
        counters["completed"] += 1
        completed = counters["completed"]
        if passed:
            counters["spoofed"] += 1
            print(f"  [{completed:4d}/{target_count}] [SPOOFED!] {fname}: confidence={conf}, code={code}")
        else:
            counters["blocked"] += 1
            if completed <= 30 or completed % 50 == 0 or completed == target_count:
                print(f"  [{completed:4d}/{target_count}] [BLOCKED]  {fname}: confidence={conf}, code={code}")

        if completed % 100 == 0:
            elapsed = time.time() - counters["start_time"]
            rate = completed / elapsed
            remaining = (target_count - completed) / rate
            print(f"  --- Progress: {completed}/{target_count} ({completed/target_count*100:.0f}%) | "
                  f"Blocked: {counters['blocked']} | Spoofed: {counters['spoofed']} | "
                  f"~{remaining:.0f}s remaining ---")

    return result_entry

def compute_stats(values):
    """Compute descriptive statistics for a list of numeric values."""
    if not values:
        return {}
    return {
        "mean": round(statistics.mean(values), 2),
        "median": round(statistics.median(values), 2),
        "stdev": round(statistics.stdev(values), 2) if len(values) > 1 else 0,
        "min": round(min(values), 2),
        "max": round(max(values), 2),
        "count": len(values),
    }

def main():
    parser = argparse.ArgumentParser(description="DeepFake vs VoiceIt API 3.0 Spoofing Test")
    parser.add_argument("--count", type=int, default=0,
                        help="Total number of deepfake verification attempts. "
                             "Cycles through available samples. 0 = use each sample once (default).")
    parser.add_argument("--workers", type=int, default=5,
                        help="Number of concurrent verification requests (default: 5)")
    args = parser.parse_args()

    real_files = sorted(glob.glob(os.path.join(REAL_DIR, "*.wav")))
    fake_files = sorted(glob.glob(os.path.join(FAKE_DIR, "*.mp3")))

    if len(real_files) < 3:
        print(f"ERROR: Need at least 3 real samples for enrollment, found {len(real_files)}")
        sys.exit(1)
    if not fake_files:
        print("ERROR: No deepfake samples found")
        sys.exit(1)

    target_count = args.count if args.count > 0 else len(fake_files)
    max_workers = args.workers

    print("=" * 70)
    print("DeepFake vs VoiceIt API 3.0 — Spoofing Test")
    print(f"Phrase: \"{PHRASE}\"")
    print(f"Real samples: {len(real_files)}")
    print(f"Unique deepfake VPPs: {len(fake_files)}")
    print(f"Target verification attempts: {target_count}")
    print(f"Concurrency: {max_workers} workers")
    if target_count > len(fake_files):
        print(f"  (cycling through {len(fake_files)} samples, ~{target_count // len(fake_files)} passes each)")
    print("=" * 70)

    # Step 1: Create user
    print("\n--- Step 1: Create User ---")
    user_id = create_user()
    if not user_id:
        print("ERROR: Failed to create user")
        sys.exit(1)

    try:
        # Step 2: Enroll with real voice samples (first 3) — sequential
        print("\n--- Step 2: Enroll with Real Voice Samples ---")
        enrollment_files = real_files[:3]
        for audio in enrollment_files:
            result = enroll_voice(user_id, audio)
            if result.get("responseCode") != "SUCC":
                print(f"  WARNING: Enrollment issue — {result}")
            time.sleep(0.5)

        # Step 3: Verify with remaining real sample(s) as control — sequential
        print("\n--- Step 3: Control — Verify with Real Voice ---")
        control_files = real_files[3:] if len(real_files) > 3 else [real_files[0]]
        real_results = []
        for audio in control_files:
            data = verify_voice(user_id, audio)
            fname = os.path.basename(audio)
            conf = data.get("confidence", "N/A")
            code = data.get("responseCode", "UNKNOWN")
            passed = code == "SUCC"
            result_entry = {"file": fname, "confidence": conf, "code": code, "passed": passed}
            if "siv1Confidence" in data:
                result_entry["siv1Confidence"] = data["siv1Confidence"]
            if "siv2Confidence" in data:
                result_entry["siv2Confidence"] = data["siv2Confidence"]
            if "textConfidence" in data:
                result_entry["textConfidence"] = data["textConfidence"]
            real_results.append(result_entry)
            status = "PASS" if passed else "FAIL"
            print(f"  [{status}] {fname}: confidence={conf}, code={code}")
            time.sleep(0.5)

        # Step 4: Spoof attempt — concurrent verification with deepfake VPPs
        print(f"\n--- Step 4: Spoof Attempt — {target_count} Deepfake VPPs ({max_workers} concurrent) ---")
        start_time = time.time()
        counters = {"completed": 0, "blocked": 0, "spoofed": 0, "start_time": start_time}

        # Build work list
        fake_cycle = cycle(fake_files)
        work_items = [(i, next(fake_cycle)) for i in range(1, target_count + 1)]

        fake_results = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(verify_worker, user_id, attempt_num, audio_path, target_count, counters): attempt_num
                for attempt_num, audio_path in work_items
            }
            for future in as_completed(futures):
                try:
                    result = future.result()
                    fake_results.append(result)
                except Exception as e:
                    attempt_num = futures[future]
                    print(f"  [ERROR] Attempt {attempt_num}: {e}")

        # Sort results by attempt number for consistent output
        fake_results.sort(key=lambda r: r["attempt"])

        elapsed_total = time.time() - start_time
        spoof_success = counters["spoofed"]
        spoof_fail = counters["blocked"]

        # Collect confidence scores for statistics
        confidence_scores = [r["confidence"] for r in fake_results if isinstance(r["confidence"], (int, float))]
        siv1_scores = [r["siv1Confidence"] for r in fake_results if "siv1Confidence" in r and isinstance(r["siv1Confidence"], (int, float))]
        siv2_scores = [r["siv2Confidence"] for r in fake_results if "siv2Confidence" in r and isinstance(r["siv2Confidence"], (int, float))]
        text_scores = [r["textConfidence"] for r in fake_results if "textConfidence" in r and isinstance(r["textConfidence"], (int, float))]

        # Per-sample aggregation (group results by source file)
        per_sample = {}
        for r in fake_results:
            fname = r["file"]
            if fname not in per_sample:
                per_sample[fname] = {"attempts": 0, "passed": 0, "blocked": 0, "confidences": [], "siv1": [], "siv2": []}
            per_sample[fname]["attempts"] += 1
            if r["passed"]:
                per_sample[fname]["passed"] += 1
            else:
                per_sample[fname]["blocked"] += 1
            if isinstance(r.get("confidence"), (int, float)):
                per_sample[fname]["confidences"].append(r["confidence"])
            if isinstance(r.get("siv1Confidence"), (int, float)):
                per_sample[fname]["siv1"].append(r["siv1Confidence"])
            if isinstance(r.get("siv2Confidence"), (int, float)):
                per_sample[fname]["siv2"].append(r["siv2Confidence"])

        per_sample_summary = {}
        for fname, data in sorted(per_sample.items()):
            entry = {
                "attempts": data["attempts"],
                "passed": data["passed"],
                "blocked": data["blocked"],
                "rejectionRate": round(data["blocked"] / data["attempts"] * 100, 1),
            }
            if data["confidences"]:
                entry["confidence"] = compute_stats(data["confidences"])
            if data["siv1"]:
                entry["siv1"] = compute_stats(data["siv1"])
            if data["siv2"]:
                entry["siv2"] = compute_stats(data["siv2"])
            per_sample_summary[fname] = entry

        # Results summary
        rejection_rate = (spoof_fail / len(fake_results) * 100) if fake_results else 0

        print("\n" + "=" * 70)
        print("RESULTS SUMMARY")
        print("=" * 70)
        print(f"Real verifications passed:     {sum(1 for r in real_results if r['passed'])}/{len(real_results)}")
        print(f"Total deepfake attempts:       {len(fake_results)}")
        print(f"Deepfake spoofs BLOCKED:       {spoof_fail}/{len(fake_results)}")
        print(f"Deepfake spoofs SUCCEEDED:     {spoof_success}/{len(fake_results)}")
        print(f"Deepfake rejection rate:       {rejection_rate:.1f}%")
        print(f"Concurrency:                   {max_workers} workers")
        print(f"Elapsed time:                  {elapsed_total:.1f}s")
        print("=" * 70)

        if confidence_scores:
            print(f"\nConfidence Statistics (n={len(confidence_scores)}):")
            print(f"  Mean:   {statistics.mean(confidence_scores):.2f}")
            print(f"  Median: {statistics.median(confidence_scores):.2f}")
            print(f"  Stdev:  {statistics.stdev(confidence_scores):.2f}" if len(confidence_scores) > 1 else "")
            print(f"  Min:    {min(confidence_scores):.2f}")
            print(f"  Max:    {max(confidence_scores):.2f}")

        if siv1_scores:
            print(f"\nVoice-Engine 1 Statistics (n={len(siv1_scores)}):")
            print(f"  Mean:   {statistics.mean(siv1_scores):.2f}")
            print(f"  Median: {statistics.median(siv1_scores):.2f}")
            print(f"  Stdev:  {statistics.stdev(siv1_scores):.2f}" if len(siv1_scores) > 1 else "")
            print(f"  Min:    {min(siv1_scores):.2f}")
            print(f"  Max:    {max(siv1_scores):.2f}")

        if siv2_scores:
            print(f"\nVoice-Engine 2 Statistics (n={len(siv2_scores)}):")
            print(f"  Mean:   {statistics.mean(siv2_scores):.2f}")
            print(f"  Median: {statistics.median(siv2_scores):.2f}")
            print(f"  Stdev:  {statistics.stdev(siv2_scores):.2f}" if len(siv2_scores) > 1 else "")
            print(f"  Min:    {min(siv2_scores):.2f}")
            print(f"  Max:    {max(siv2_scores):.2f}")

        if spoof_success > 0:
            print(f"\nWARNING: {spoof_success} deepfake(s) passed verification!")
            for r in fake_results:
                if r["passed"]:
                    print(f"  - attempt {r['attempt']}: {r['file']} (confidence: {r['confidence']})")
        else:
            print("\nAll deepfakes were rejected. VoiceIt biometric verification held up.")

        # Real voice stats
        real_siv1 = [r["siv1Confidence"] for r in real_results if "siv1Confidence" in r and isinstance(r["siv1Confidence"], (int, float))]
        real_siv2 = [r["siv2Confidence"] for r in real_results if "siv2Confidence" in r and isinstance(r["siv2Confidence"], (int, float))]

        # Save results
        output = {
            "timestamp": datetime.now().isoformat(),
            "phrase": PHRASE,
            "contentLanguage": CONTENT_LANG,
            "userId": user_id,
            "targetCount": target_count,
            "uniqueSamples": len(fake_files),
            "concurrency": max_workers,
            "enrollmentFiles": [os.path.basename(f) for f in enrollment_files],
            "elapsedSeconds": round(elapsed_total, 1),
            "realVerifications": real_results,
            "deepfakeVerifications": fake_results,
            "perSampleSummary": per_sample_summary,
            "summary": {
                "realPassCount": sum(1 for r in real_results if r["passed"]),
                "realTotal": len(real_results),
                "fakeBlockedCount": spoof_fail,
                "fakeSpoofedCount": spoof_success,
                "fakeTotal": len(fake_results),
                "rejectionRate": rejection_rate,
                "confidence": compute_stats(confidence_scores) if confidence_scores else {},
                "siv1": compute_stats(siv1_scores) if siv1_scores else {},
                "siv2": compute_stats(siv2_scores) if siv2_scores else {},
                "textConfidence": compute_stats(text_scores) if text_scores else {},
                "engines": {
                    "real": {
                        "siv1": real_siv1[0] if len(real_siv1) == 1 else (compute_stats(real_siv1) if real_siv1 else "N/A"),
                        "siv2": real_siv2[0] if len(real_siv2) == 1 else (compute_stats(real_siv2) if real_siv2 else "N/A"),
                    },
                    "deepfake": {
                        "siv1": compute_stats(siv1_scores) if siv1_scores else {},
                        "siv2": compute_stats(siv2_scores) if siv2_scores else {},
                    },
                },
            },
        }
        out_path = os.path.join(os.path.dirname(__file__), "spoof_test_results.json")
        with open(out_path, "w") as f:
            json.dump(output, f, indent=2)
        print(f"\nResults saved to {out_path}")

    finally:
        # Cleanup: delete user
        print("\n--- Cleanup ---")
        delete_user(user_id)

if __name__ == "__main__":
    main()
