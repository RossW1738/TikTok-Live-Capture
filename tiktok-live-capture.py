import tkinter as tk
from tkinter import scrolledtext, messagebox, ttk
import subprocess
import threading
import os
import re
import winsound
import time
from datetime import datetime, timedelta
import json
from urllib.parse import quote
import uuid
import requests
try:
    from curl_cffi import requests as cffi_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    cffi_requests = None
    CURL_CFFI_AVAILABLE = False
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    sync_playwright = None
    PLAYWRIGHT_AVAILABLE = False

# ── User Configuration ──
# Update these paths for your own machine before running.
BASE_DIR = os.path.join(os.path.expanduser("~"), "TikTokLiveRecorder")
OUTPUT_DIR = os.path.join(BASE_DIR, "recordings")
TRANSCRIPT_DIR = os.path.join(OUTPUT_DIR, "_transcript_chunks")  # temp audio chunks, auto-cleaned
ORPHAN_DIR = os.path.join(OUTPUT_DIR, "_ORPHAN_RECOVERED")  # raw .flv leftovers moved here after startup recovery
FFMPEG = "ffmpeg"
RECORDER_LOG_DIR = os.path.join(BASE_DIR, "logs")  # shutdown log dump target
DATA_DIR = os.path.join(BASE_DIR, "data")  # fixed location, independent of where rec.py lives
os.makedirs(DATA_DIR, exist_ok=True)
TARGETS_FILE = os.path.join(DATA_DIR, "monitored_targets.json")
QUALITY_SETTINGS_FILE = os.path.join(DATA_DIR, "quality_settings.json")

# ── Global Shutdown, Export & Debug Logging Buffers ──
is_shutting_down = False
export_lock = threading.Lock()
active_exports = set()
full_debug_log_buffer = []  # Retains raw technical stdout lines for shutdown logs

def track_export_start(export_id):
    with export_lock:
        active_exports.add(export_id)

def track_export_end(export_id):
    with export_lock:
        active_exports.discard(export_id)

# ── Quality Tier System ──
QUALITY_TIERS = [
    ("origin", 1, "Best (Origin)"),
    ("uhd",    2, "High"),
    ("hd",     3, "Medium"),
    ("sd",     4, "Low"),
    ("zsd",    5, "Data Saver"),
]
LD_RANK = 6  # hidden from the UI, still classified internally
QUALITY_RANK = {key: rank for key, rank, _label in QUALITY_TIERS}
QUALITY_LABEL = {key: label for key, _rank, label in QUALITY_TIERS}
QUALITY_LABELS_ORDERED = [label for _key, _rank, label in QUALITY_TIERS]
QUALITY_LABEL_TO_KEY = {label: key for key, _rank, label in QUALITY_TIERS}
QUALITY_RANK_TO_LABEL = {rank: label for _key, rank, label in QUALITY_TIERS}

GLOBAL_QUALITY_SETTINGS = {"preferred": "origin", "allow_lower": False}

# ── HTTP stream-URL fetcher configuration ──
COOKIES_FILE = os.path.join(DATA_DIR, "cookies.json")
PLAYWRIGHT_PROFILE_DIR = os.path.join(DATA_DIR, "playwright_profile")
HTTP_POLLER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# Must match a profile curl_cffi actually ships (see curl_cffi.requests.BrowserType);
# keep this in step with the Chrome version in the UA string above.
CURL_CFFI_IMPERSONATE = "chrome131"
HTTP_POLLER_HYDRATION_IDS = ["SIGI_STATE", "__UNIVERSAL_DATA_FOR_REHYDRATION__"]
CHALLENGE_PAGE_DETAIL = "no hydration data found (likely a bot-check challenge page)"

# ── Polling Interval Presets ──
POLL_INTERVAL_PRESETS = [15, 30, 60, 120, 300]
poll_interval_index = 1  # Default: 30s
poll_interval_seconds = POLL_INTERVAL_PRESETS[poll_interval_index]

http_request_lock = threading.Lock()

active_tasks = {}
inactive_targets_ui = {}     # username.lower() -> dict of UI elements
monitored_targets_data = {}  # Persistent target configs: username.lower() -> dict
task_counter = 0
transcribe_active = datetime.now().hour < 17

whisper_model = None
whisper_model_lock = threading.Lock()
TRANSCRIBE_CHUNK_SECONDS = 15

transcript_log = []
transcript_seq_counter = 0
transcript_log_lock = threading.Lock()
TRANSCRIPT_LOG_MAX = 1000
debug_visible = False


def is_valid_tiktok_url(url):
    return True


def get_output_path(username=None, task_id=None):
    """Generates a 100% unique output filepath including a task suffix to prevent collision."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = f"tiktok_{username}_" if username else "tiktok_"
    suffix = f"_{task_id}" if task_id else ""
    return os.path.join(OUTPUT_DIR, f"{prefix}{timestamp}{suffix}.flv")


def log(msg, color=None):
    try:
        ts = datetime.now().strftime("%H:%M:%S")
        full_debug_log_buffer.append(f"[{ts}] {msg}")
        log_box.config(state="normal")
        log_box.insert(tk.END, msg + "\n", color or "normal")
        log_box.see(tk.END)
        log_box.config(state="disabled")
    except Exception:
        pass


def set_task_sub_status(task_id, text):
    """Safely updates the amber in-row status indicator on the main UI thread."""
    task = active_tasks.get(task_id)
    if task and task.get("sub_status_var"):
        root.after(0, lambda: task["sub_status_var"].set(text))


def alert_recording_ended(username, reason, task_id=None):
    if is_shutting_down:
        return
    stop_beep = threading.Event()

    if task_id and task_id in active_tasks:
        active_tasks[task_id]["giveup_beep_event"] = stop_beep

    def beep_loop():
        deadline = time.time() + 90
        while not stop_beep.is_set() and time.time() < deadline:
            winsound.Beep(1000, 400)
            stop_beep.wait(0.6)

    beep_thread = threading.Thread(target=beep_loop, daemon=True)
    beep_thread.start()

    def show_dialog():
        messagebox.showwarning(
            "Recording Stopped",
            f"Recording for @{username or 'unknown'} ended unexpectedly!\n\nReason: {reason}\n\nCheck the log for details."
        )
        stop_beep.set()

    root.after(0, show_dialog)


def alert_error_line():
    if is_shutting_down:
        return
    def _beep():
        winsound.Beep(900, 250)
        time.sleep(0.1)
        winsound.Beep(900, 250)
    threading.Thread(target=_beep, daemon=True).start()


def get_whisper_model():
    global whisper_model
    with whisper_model_lock:
        if whisper_model is None:
            from faster_whisper import WhisperModel
            root.after(0, lambda: log("[Transcribe] Loading Whisper model (first run only, may take a moment)...", "normal"))
            try:
                whisper_model = WhisperModel("small.en", device="cuda", compute_type="float16")
            except Exception:
                whisper_model = WhisperModel("small.en", device="cpu", compute_type="int8")
    return whisper_model


def watch_transcript_segments(task_id):
    task = active_tasks.get(task_id)
    if not task:
        return
    seg_dir = task["transcript_seg_dir"]
    transcript_path = task["transcript_file"]
    pref = task["log_prefix"]

    try:
        model = get_whisper_model()
    except Exception as e:
        root.after(0, lambda: log(f"{pref}Live transcription disabled: {e}", "warn"))
        return

    processed = set()

    def transcribe_chunk(fname):
        fpath = os.path.join(seg_dir, fname)
        text = ""
        try:
            segments, _info = model.transcribe(fpath, language="en", vad_filter=True)
            text = " ".join(seg.text.strip() for seg in segments).strip()
        except Exception as e:
            root.after(0, lambda: log(f"{pref}Transcription error on {fname}: {e}", "warn"))
        if text:
            ts = datetime.now().strftime("%H:%M:%S")
            line = f"[{ts}] {text}"
            try:
                with open(transcript_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception as e:
                root.after(0, lambda: log(f"{pref}Failed writing transcript: {e}", "warn"))
            root.after(0, lambda l=line: log(f"{pref}[Transcript] {l}", "normal"))

            global transcript_seq_counter
            with transcript_log_lock:
                transcript_seq_counter += 1
                transcript_log.append({
                    "seq": transcript_seq_counter,
                    "ts": ts,
                    "username": task.get("username") or "",
                    "task_id": task_id,
                    "text": text
                })
                if len(transcript_log) > TRANSCRIPT_LOG_MAX:
                    del transcript_log[: len(transcript_log) - TRANSCRIPT_LOG_MAX]
        try:
            os.remove(fpath)
        except Exception:
            pass

    while task_id in active_tasks and not active_tasks[task_id]["user_stopped"]:
        try:
            files = sorted(f for f in os.listdir(seg_dir) if f.endswith(".wav"))
        except FileNotFoundError:
            time.sleep(1)
            continue

        for fname in files[:-1] if len(files) > 1 else []:
            if fname not in processed:
                processed.add(fname)
                transcribe_chunk(fname)

        time.sleep(2)

    try:
        remaining = sorted(f for f in os.listdir(seg_dir) if f.endswith(".wav"))
    except FileNotFoundError:
        remaining = []
    for fname in remaining:
        if fname not in processed:
            transcribe_chunk(fname)

    try:
        os.rmdir(seg_dir)
    except Exception:
        pass


def remux_to_mp4(flv_path, task_id, error_tolerant=False):
    export_id = f"remux_{task_id}_{time.time()}"
    track_export_start(export_id)
    try:
        mp4_path = flv_path.replace(".flv", ".mp4")
        
        prefix = "[System] "
        task = active_tasks.get(task_id)
        if task:
            prefix = task["log_prefix"]
            set_task_sub_status(task_id, "⚙ Remuxing to MP4...")
            root.after(0, lambda tid=task_id: active_tasks.get(tid, {}).get("status_var", tk.StringVar()).set("Converting to MP4..."))

        root.after(0, lambda pref=prefix: log(f"{pref}Converting to MP4...", "normal"))

        cmd = [FFMPEG]
        if error_tolerant:
            cmd.extend(["-err_detect", "ignore_err", "-fflags", "+genpts+igndts"])
        cmd.extend([
            "-i", flv_path,
            "-c", "copy",
            "-color_range", "tv",
            mp4_path
        ])

        try:
            result = subprocess.run(cmd, capture_output=True, timeout=600)
            if result.returncode == 0:
                try:
                    os.remove(flv_path)
                except Exception:
                    pass
                root.after(0, lambda pref=prefix: log(f"{pref}Done! Saved: {mp4_path}", "good"))
                set_task_sub_status(task_id, "✔ Finished")
                if task_id in active_tasks:
                    if not active_tasks[task_id].get("is_auto_monitor"):
                        root.after(0, lambda tid=task_id: active_tasks.get(tid, {}).get("status_var", tk.StringVar()).set("Finished (MP4 Saved)"))
            else:
                root.after(0, lambda pref=prefix: log(f"{pref}Conversion failed, keeping FLV: {flv_path}", "warn"))
                set_task_sub_status(task_id, "⚠ FLV Kept")
                if task_id in active_tasks:
                    if not active_tasks[task_id].get("is_auto_monitor"):
                        root.after(0, lambda tid=task_id: active_tasks.get(tid, {}).get("status_var", tk.StringVar()).set("Failed (FLV Kept)"))
        except subprocess.TimeoutExpired:
            root.after(0, lambda pref=prefix: log(f"{pref}Conversion timed out, keeping FLV.", "warn"))
            set_task_sub_status(task_id, "⚠ Timed Out")
            if task_id in active_tasks:
                if not active_tasks[task_id].get("is_auto_monitor"):
                    root.after(0, lambda tid=task_id: active_tasks.get(tid, {}).get("status_var", tk.StringVar()).set("Timed Out"))
        except Exception as e:
            root.after(0, lambda pref=prefix: log(f"{pref}Conversion error: {e}", "warn"))
            set_task_sub_status(task_id, "⚠ Error")
            if task_id in active_tasks:
                if not active_tasks[task_id].get("is_auto_monitor"):
                    root.after(0, lambda tid=task_id: active_tasks.get(tid, {}).get("status_var", tk.StringVar()).set("Error"))
    finally:
        track_export_end(export_id)


MERGE_MAX_SEGMENT_SECONDS = 60
MERGE_MAX_GAP_SECONDS = 30


def merge_short_segments(session_segments, log_prefix="[System] "):
    export_id = f"merge_{time.time()}"
    track_export_start(export_id)
    try:
        segments = sorted(session_segments, key=lambda s: s["start"])
        if len(segments) < 2:
            return

        time.sleep(3)

        merged = [dict(segments[0])]
        for seg in segments[1:]:
            prev = merged[-1]
            seg_duration = seg["end"] - seg["start"]
            gap = seg["start"] - prev["end"]

            prev_mp4 = prev["mp4_path"]
            seg_mp4 = seg["mp4_path"]

            can_merge = (
                seg_duration < MERGE_MAX_SEGMENT_SECONDS
                and 0 <= gap <= MERGE_MAX_GAP_SECONDS
                and os.path.exists(prev_mp4)
                and os.path.exists(seg_mp4)
            )

            if not can_merge:
                merged.append(dict(seg))
                continue

            combined_path = prev_mp4 + ".merging.mp4"
            list_path = prev_mp4 + ".concat.txt"
            try:
                with open(list_path, "w", encoding="utf-8") as f:
                    f.write(f"file '{prev_mp4}'\n")
                    f.write(f"file '{seg_mp4}'\n")

                cmd = [FFMPEG, "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", combined_path]
                result = subprocess.run(cmd, capture_output=True, timeout=120)

                if result.returncode == 0 and os.path.exists(combined_path):
                    os.replace(combined_path, prev_mp4)
                    try:
                        os.remove(seg_mp4)
                    except Exception:
                        pass
                    prev["end"] = seg["end"]
                    root.after(0, lambda pref=log_prefix, a=os.path.basename(seg_mp4): log(
                        f"{pref}Merged short segment ({a}, {seg_duration:.1f}s) into preceding clip.", "good"))
                else:
                    root.after(0, lambda pref=log_prefix: log(
                        f"{pref}Segment merge failed, leaving clips separate.", "warn"))
                    merged.append(dict(seg))
            except Exception as e:
                root.after(0, lambda pref=log_prefix, err=e: log(f"{pref}Segment merge error: {err}", "warn"))
                merged.append(dict(seg))
            finally:
                for tmp in (list_path, combined_path):
                    if os.path.exists(tmp):
                        try:
                            os.remove(tmp)
                        except Exception:
                            pass
    finally:
        track_export_end(export_id)


def recover_orphaned_flvs():
    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        os.makedirs(ORPHAN_DIR, exist_ok=True)
        candidates = [
            f for f in os.listdir(OUTPUT_DIR)
            if f.lower().endswith(".flv") and os.path.isfile(os.path.join(OUTPUT_DIR, f))
        ]
    except Exception as e:
        root.after(0, lambda err=e: log(f"[Recovery] Could not scan {OUTPUT_DIR}: {err}", "warn"))
        return

    orphans = []
    for fname in candidates:
        flv_path = os.path.join(OUTPUT_DIR, fname)
        mp4_path = flv_path[:-4] + ".mp4"
        if not os.path.exists(mp4_path):
            orphans.append(flv_path)

    if not orphans:
        return

    root.after(0, lambda n=len(orphans): log(f"[Recovery] Found {n} leftover .flv file(s) from a previous session. Attempting recovery...", "warn"))

    recovered, failed = 0, 0
    for flv_path in orphans:
        base = os.path.basename(flv_path)
        mp4_path = flv_path[:-4] + ".mp4"
        cmd = [
            FFMPEG, "-err_detect", "ignore_err", "-fflags", "+genpts+igndts",
            "-i", flv_path, "-c", "copy", "-color_range", "tv", mp4_path
        ]
        ok = False
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=300)
            ok = result.returncode == 0 and os.path.exists(mp4_path)
        except Exception:
            ok = False

        if ok:
            recovered += 1
            root.after(0, lambda b=base: log(f"[Recovery] Recovered: {b} -> .mp4", "good"))
        else:
            failed += 1
            if os.path.exists(mp4_path):
                try:
                    os.remove(mp4_path)
                except Exception:
                    pass
            root.after(0, lambda b=base: log(f"[Recovery] Could not remux {b} (kept in _ORPHAN_RECOVERED for manual review).", "warn"))

        moved = False
        for attempt in range(5):
            try:
                dest = os.path.join(ORPHAN_DIR, base)
                if os.path.exists(dest):
                    dest = os.path.join(ORPHAN_DIR, f"{int(time.time())}_{base}")
                os.replace(flv_path, dest)
                moved = True
                break
            except OSError:
                time.sleep(0.5)

        if not moved:
            root.after(0, lambda b=base: log(f"[Recovery] Note: File {b} is still locked by system, kept in main directory.", "warn"))

    root.after(0, lambda r=recovered, fl=failed: log(f"[Recovery] Done: {r} recovered, {fl} failed (raw files moved to _ORPHAN_RECOVERED).", "good" if failed == 0 else "warn"))


def get_quality_label(url):
    rank = _classify_stream_tier(url)
    if rank in QUALITY_RANK_TO_LABEL:
        return QUALITY_RANK_TO_LABEL[rank]
    elif rank == LD_RANK:
        return "Lowest (unreliable)"
    else:
        return f"Unrecognized quality (rank {rank})"


def log_stream_summary(task_info, exit_code, recent_lines):
    """Outputs a clean, human-readable summary box for the stream segment in UI, retaining full technical logs for shutdown dumps."""
    username = task_info.get("username") or "Manual"
    duration_s = int(time.time() - (task_info.get("start_time") or time.time()))
    m, s = divmod(duration_s, 60)
    h, m = divmod(m, 60)
    dur_str = f"{h}h {m}m {s}s" if h > 0 else (f"{m}m {s}s" if m > 0 else f"{s}s")

    file_size_str = "Unknown"
    flv_path = task_info.get("output_file")
    if flv_path and os.path.exists(flv_path):
        size_mb = os.path.getsize(flv_path) / (1024 * 1024)
        file_size_str = f"{size_mb:.1f} MB"

    last_text = " ".join(recent_lines).lower()
    if task_info.get("user_stopped"):
        status_text = "Stopped by User (Manual Cancel)"
    elif exit_code in (0, 255):
        status_text = "Host Ended Live Stream (Clean Disconnect)"
    elif "404" in last_text or "not found" in last_text:
        status_text = "404 Not Found (Stream URL Expired)"
    elif "403" in last_text or "forbidden" in last_text:
        status_text = "403 Forbidden (URL Revoked)"
    else:
        status_text = f"Connection Interrupted (Exit Code: {exit_code})"

    root.after(0, lambda: log("======================================================================", "normal"))
    root.after(0, lambda: log(f"  STREAM SUMMARY: [@{username}]", "good"))
    root.after(0, lambda: log("----------------------------------------------------------------------", "normal"))
    root.after(0, lambda: log(f"  • Status       : {status_text}", "good" if exit_code in (0, 255) else "warn"))
    root.after(0, lambda: log(f"  • Quality      : {get_quality_label(task_info['url'])}", "normal"))
    root.after(0, lambda: log(f"  • Duration     : {dur_str}", "normal"))
    root.after(0, lambda: log(f"  • Output Size  : {file_size_str}", "normal"))
    root.after(0, lambda: log(f"  • File Path    : {flv_path}", "normal"))
    root.after(0, lambda: log("======================================================================", "normal"))

    # Record full technical debug details in background log buffer for shutdown dumps
    pref = task_info.get("log_prefix", "")
    debug_text = (
        f"\n--- TECHNICAL DEBUG ENTRY [{pref}] ---\n"
        f"  Stream URL : {task_info.get('url')}\n"
        f"  Exit code  : {exit_code}\n"
        f"  Reason     : {status_text}\n"
        f"  Recent FFmpeg stdout lines:\n" +
        "\n".join(f"    {l}" for l in recent_lines) +
        "\n----------------------------------------------------\n"
    )
    full_debug_log_buffer.append(debug_text)

    return status_text


# ── Persistence & Target Management ──

def load_monitored_targets():
    global monitored_targets_data
    if os.path.exists(TARGETS_FILE):
        try:
            with open(TARGETS_FILE, "r", encoding="utf-8") as f:
                monitored_targets_data = json.load(f)
            log(f"[Persistence] Loaded {len(monitored_targets_data)} target(s) from monitored_targets.json.", "good")
        except Exception as e:
            log(f"[Persistence] Error loading monitored_targets.json: {e}", "warn")


def check_active_scheduled_targets_on_startup():
    active_count = 0
    for key, cfg in monitored_targets_data.items():
        if "skip_until" in cfg:
            cfg["skip_until"] = 0
        if cfg.get("mode") == "automatic" and is_target_in_active_schedule(cfg):
            active_count += 1
            u = cfg.get("username", key)
            root.after(0, lambda name=u: log(f"[Scheduler] Active schedule window match on launch for @{name}. Initiating check...", "good"))
    save_monitored_targets()


def save_monitored_targets():
    try:
        with open(TARGETS_FILE, "w", encoding="utf-8") as f:
            json.dump(monitored_targets_data, f, indent=2)
    except Exception as e:
        log(f"[Persistence] Error saving monitored_targets.json: {e}", "warn")


def persist_transcribe_preference(username, enabled):
    """Record the transcribe on/off state used for a manual Fetch Live recording
    into that single target's saved settings, so if they're later switched to
    Favorite/Automatic mode the transcribe checkbox already matches. Only touches
    this one username's entry - never affects any other saved target."""
    key = username.lower()
    if key in monitored_targets_data:
        monitored_targets_data[key]["transcribe"] = enabled
    else:
        monitored_targets_data[key] = {
            "username": username,
            "mode": "favorite",
            "interval": poll_interval_seconds,
            "schedule_enabled": False,
            "schedules": [{"start_time": "05:00 PM", "end_time": "11:30 PM"}],
            "preferred_quality": GLOBAL_QUALITY_SETTINGS["preferred"],
            "allow_lower": GLOBAL_QUALITY_SETTINGS["allow_lower"],
            "transcribe": enabled
        }
    save_monitored_targets()


def load_quality_settings():
    if os.path.exists(QUALITY_SETTINGS_FILE):
        try:
            with open(QUALITY_SETTINGS_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if loaded.get("preferred") in QUALITY_RANK:
                GLOBAL_QUALITY_SETTINGS["preferred"] = loaded["preferred"]
            GLOBAL_QUALITY_SETTINGS["allow_lower"] = bool(loaded.get("allow_lower", False))
        except Exception as e:
            log(f"[Persistence] Error loading quality_settings.json: {e}", "warn")


def save_quality_settings():
    try:
        with open(QUALITY_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(GLOBAL_QUALITY_SETTINGS, f, indent=2)
    except Exception as e:
        log(f"[Persistence] Error saving quality_settings.json: {e}", "warn")


def get_effective_quality_settings(username):
    if username:
        cfg = monitored_targets_data.get(username.lower())
        if cfg:
            preferred_key = cfg.get("preferred_quality", GLOBAL_QUALITY_SETTINGS["preferred"])
            allow_lower = cfg.get("allow_lower", GLOBAL_QUALITY_SETTINGS["allow_lower"])
            return QUALITY_RANK.get(preferred_key, QUALITY_RANK["origin"]), allow_lower
    return QUALITY_RANK.get(GLOBAL_QUALITY_SETTINGS["preferred"], QUALITY_RANK["origin"]), GLOBAL_QUALITY_SETTINGS["allow_lower"]


def is_within_12h_schedule(start_str, end_str):
    try:
        now = datetime.now().time()
        t_start = datetime.strptime(start_str.strip(), "%I:%M %p").time()
        t_end = datetime.strptime(end_str.strip(), "%I:%M %p").time()
        if t_start <= t_end:
            return t_start <= now <= t_end
        else:
            return now >= t_start or now <= t_end
    except Exception:
        return True


def get_current_schedule_end_timestamp(cfg):
    schedules = cfg.get("schedules")
    if not schedules:
        s_time = cfg.get("start_time", "05:00 PM")
        e_time = cfg.get("end_time", "11:30 PM")
        schedules = [{"start_time": s_time, "end_time": e_time}]

    now_dt = datetime.now()
    now_time = now_dt.time()

    for sched in schedules:
        s_str = sched.get("start_time", "05:00 PM")
        e_str = sched.get("end_time", "11:30 PM")
        if is_within_12h_schedule(s_str, e_str):
            try:
                t_start = datetime.strptime(s_str.strip(), "%I:%M %p").time()
                t_end = datetime.strptime(e_str.strip(), "%I:%M %p").time()
                
                if t_start <= t_end:
                    end_dt = datetime.combine(now_dt.date(), t_end)
                else:
                    if now_time >= t_start:
                        end_dt = datetime.combine(now_dt.date(), t_end) + timedelta(days=1)
                    else:
                        end_dt = datetime.combine(now_dt.date(), t_end)

                return end_dt.timestamp()
            except Exception:
                pass

    return None


def is_target_in_active_schedule(cfg):
    skip_until = cfg.get("skip_until", 0)
    if time.time() < skip_until:
        return False

    if not cfg.get("schedule_enabled", False):
        return True

    schedules = cfg.get("schedules")
    if not schedules:
        s_time = cfg.get("start_time", "05:00 PM")
        e_time = cfg.get("end_time", "11:30 PM")
        schedules = [{"start_time": s_time, "end_time": e_time}]

    for sched in schedules:
        s_str = sched.get("start_time", "05:00 PM")
        e_str = sched.get("end_time", "11:30 PM")
        if is_within_12h_schedule(s_str, e_str):
            return True

    return False


# ── Core Recording & Monitoring ──

def start_recording(url=None, username=None, force_transcribe=None):
    global task_counter

    if is_shutting_down:
        log("[Shutdown] System shutdown in progress. Recording start blocked.", "warn")
        return

    if not url:
        log("Use Fetch Live to look up a handle and start recording.", "warn")
        return

    if not username:
        username = "Manual"

    if username and username != "Manual":
        for tid, task in list(active_tasks.items()):
            if task["username"] and task["username"].lower() == username.lower():
                if task["proc"] and (task["proc"].poll() is None or task["status_var"].get().startswith("Retrying") or task.get("needs_new_url")):
                    if task["url"] != url:
                        task["url"] = url
                        task["needs_new_url"] = False
                        log(f"[Link] Updated stream URL for active @{username} to: {url}", "good")
                    else:
                        log(f"Already recording or reconnecting for @{username} (Task {tid}).", "warn")
                    return

    for tid, task in list(active_tasks.items()):
        if task["url"] == url and task["proc"] and task["proc"].poll() is None:
            log(f"Already recording this URL (Task {tid}).", "warn")
            return

    task_counter += 1
    task_id = f"task_{task_counter}"

    task_info = {
        "proc": None,
        "url": url,
        "username": username,
        "output_file": None,
        "user_stopped": False,
        "stop_reason": None,
        "session_segments": [],
        "confirmed_offline": False,
        "status_var": tk.StringVar(value="Connecting..."),
        "sub_status_var": tk.StringVar(value=""),  # Blank during connection/recording
        "row_frame": None,
        "stop_btn": None,
        "log_prefix": f"[@{username}] " if username else "[Manual] ",
        "start_time": None,
        "needs_new_url": False,
        "transcript_seg_dir": None,
        "transcript_file": None,
        "dead_stream_ids": set(),
        "is_auto_monitor": False
    }

    active_tasks[task_id] = task_info

    remove_inactive_target_ui(username)
    root.after(0, lambda: create_task_row(task_id))

    def extract_stream_id(stream_url):
        try:
            path = stream_url.split("?")[0].rsplit("/", 1)[-1]
            return path.replace(".flv", "")
        except Exception:
            return stream_url

    def run():
        max_retries = 12
        retry_delay = 5
        consecutive_failures = 0

        if force_transcribe is not None:
            transcribe_this_task = force_transcribe
        else:
            target_cfg_for_transcribe = monitored_targets_data.get((username or "").lower(), {})
            transcribe_this_task = transcribe_active and bool(username) and target_cfg_for_transcribe.get("transcribe", False)
        if transcribe_this_task:
            seg_dir = os.path.join(TRANSCRIPT_DIR, task_id)
            os.makedirs(seg_dir, exist_ok=True)
            task_info["transcript_seg_dir"] = seg_dir
            safe_name = re.sub(r'[^A-Za-z0-9_-]', '_', username or "manual")
            task_info["transcript_file"] = os.path.join(OUTPUT_DIR, f"{safe_name}_{task_id}_transcript.txt")
            threading.Thread(target=watch_transcript_segments, args=(task_id,), daemon=True).start()
            root.after(0, lambda: log(f"{task_info['log_prefix']}Live transcription enabled (~{TRANSCRIBE_CHUNK_SECONDS}s updates) -> {task_info['transcript_file']}", "good"))
        elif force_transcribe is None and transcribe_active and username:
            root.after(0, lambda: log(f"{task_info['log_prefix']}Live transcription skipped (not enabled for this target in Target Settings).", "normal"))

        while not task_info["user_stopped"] and not is_shutting_down:
            output_file = get_output_path(username, task_id)
            task_info["output_file"] = output_file

            cmd = [
                FFMPEG,
                "-referer", "https://www.tiktok.com/",
                "-user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
                "-reconnect", "1",
                "-reconnect_at_eof", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "30",
                "-i", task_info["url"],
                "-c", "copy",
                output_file
            ]

            if transcribe_this_task:
                cmd.extend([
                    "-vn", "-ac", "1", "-ar", "16000",
                    "-f", "segment", "-segment_time", str(TRANSCRIBE_CHUNK_SECONDS), "-reset_timestamps", "1",
                    os.path.join(seg_dir, "chunk_%05d.wav")
                ])

            recent_lines = []
            connected_successfully = False

            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    universal_newlines=True,
                    bufsize=1,
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
                task_info["proc"] = proc
                task_info["start_time"] = time.time()
            except Exception as e:
                log(f"Failed to start FFMPEG for @{username or 'unknown'}: {e}", "warn")
                break

            log(f"{task_info['log_prefix']}Recording started -> {output_file}", "good")
            log(f"{task_info['log_prefix']}Quality: {get_quality_label(task_info['url'])}", "good")
            log(f"{task_info['log_prefix']}Active Stream URL: {task_info['url']}", "normal")

            for line in proc.stdout:
                line = line.rstrip()
                recent_lines.append(line)
                if len(recent_lines) > 10:
                    recent_lines.pop(0)

                full_debug_log_buffer.append(f"[{task_info['log_prefix']}] {line}")

                if "time=" in line and "bitrate=" in line:
                    if not connected_successfully:
                        connected_successfully = True
                        consecutive_failures = 0
                        task_info["needs_new_url"] = False
                        set_task_sub_status(task_id, "")  # Blank during active recording
                    match = re.search(r"time=(\S+)", line)
                    if match:
                        t_str = match.group(1)
                        root.after(0, lambda tid=task_id, t=t_str: active_tasks.get(tid, {}).get("status_var", tk.StringVar()).set(f"Recording  |  {t}"))
                elif "error" in line.lower() or "warning" in line.lower():
                    noisy_keywords = ["will reconnect at", "http error 404", "tls @", "error during demuxing"]
                    if not any(k in line.lower() for k in noisy_keywords):
                        root.after(0, lambda l=line, pref=task_info["log_prefix"]: log(f"  {pref}{l}", "warn"))
                        alert_error_line()

            proc.wait()
            exit_code = proc.returncode

            stop_reason = log_stream_summary(task_info, exit_code, recent_lines)
            if "404" in stop_reason.lower():
                sid = extract_stream_id(task_info["url"])
                task_info["dead_stream_ids"].add(sid)

            segment_was_clean = exit_code in (0, 255, 1, -1)
            if segment_was_clean:
                root.after(0, lambda pref=task_info["log_prefix"]: log(f"{pref}Segment closed. Remuxing segment to MP4...", "normal"))
            else:
                root.after(0, lambda pref=task_info["log_prefix"], ec=exit_code: log(f"{pref}ffmpeg exited with code {ec} - attempting error-tolerant remux instead of discarding...", "warn"))
            
            set_task_sub_status(task_id, "⚙ Remuxing to MP4...")
            threading.Thread(
                target=remux_to_mp4,
                args=(output_file, task_id),
                kwargs={"error_tolerant": not segment_was_clean},
                daemon=True
            ).start()

            task_info.setdefault("session_segments", []).append({
                "flv_path": output_file,
                "mp4_path": output_file.replace(".flv", ".mp4"),
                "start": task_info.get("start_time", time.time()),
                "end": time.time(),
            })

            if task_info["user_stopped"] or is_shutting_down:
                break

            task_info["needs_new_url"] = True

            consecutive_failures += 1
            if consecutive_failures > max_retries:
                log(f"{task_info['log_prefix']}Max reconnection attempts reached ({max_retries}). Giving up.", "warn")
                threading.Thread(
                    target=alert_recording_ended,
                    args=(username, f"Stream disconnected (tried {max_retries} reconnect attempts)"),
                    kwargs={"task_id": task_id},
                    daemon=True
                ).start()
                break

            log(f"{task_info['log_prefix']}Connection interrupted. Signalling browser to recover link. Retrying in {retry_delay}s... (Attempt {consecutive_failures}/{max_retries})", "warn")
            set_task_sub_status(task_id, "⚡ Reconnecting")

            for _ in range(retry_delay * 10):
                if task_info["user_stopped"] or is_shutting_down:
                    break
                task_info["status_var"].set(f"Retrying ({consecutive_failures}/{max_retries})...")
                time.sleep(0.1)

            if task_info["user_stopped"] or is_shutting_down:
                break

            if username and username != "Manual":
                pref_rank, allow_lower = get_effective_quality_settings(username)
                with http_request_lock:
                    fresh_url, detail = fetch_stream_url_http(username, pref_rank, allow_lower)
                if fresh_url:
                    fresh_sid = extract_stream_id(fresh_url)
                    if fresh_sid not in task_info["dead_stream_ids"] and fresh_url != task_info["url"]:
                        task_info["url"] = fresh_url
                        task_info["needs_new_url"] = False
                        root.after(0, lambda u=username: log(f"[Recovery] Acquired fresh stream URL for @{u} via HTTP fetch.", "good"))

            wait_count = 0
            while task_info["needs_new_url"] and not task_info["user_stopped"] and not is_shutting_down:
                if wait_count == 0:
                    root.after(0, lambda pref=task_info["log_prefix"]: log(f"{pref}Waiting for fresh stream URL (Phase 1: Fast recovery 60s)...", "warn"))
                
                if wait_count <= 60:
                    task_info["status_var"].set(f"Recovery Phase 1 ({60 - wait_count}s)")
                else:
                    task_info["status_var"].set(f"Recovery Phase 2 ({300 - wait_count}s)")

                time.sleep(1)
                wait_count += 1

                should_check = (wait_count <= 60 and wait_count % 10 == 0) or (wait_count > 60 and wait_count % 15 == 0)

                if username and username != "Manual" and should_check:
                    pref_rank, allow_lower = get_effective_quality_settings(username)
                    with http_request_lock:
                        fresh_url, detail = fetch_stream_url_http(username, pref_rank, allow_lower)
                    if fresh_url:
                        fresh_sid = extract_stream_id(fresh_url)
                        if fresh_sid not in task_info["dead_stream_ids"] and fresh_url != task_info["url"]:
                            task_info["url"] = fresh_url
                            task_info["needs_new_url"] = False
                            root.after(0, lambda u=username: log(f"[Recovery] Stream revived for @{u}!", "good"))
                            break
                    elif detail and ("ended" in str(detail).lower() or "inactive" in str(detail).lower()):
                        root.after(0, lambda u=username: log(f"[Recovery] Verified @{u} is offline. Giving up on this session.", "normal"))
                        task_info["confirmed_offline"] = True
                        break

                if wait_count >= 300:
                    root.after(0, lambda pref=task_info["log_prefix"]: log(f"{pref}5-minute revival window expired.", "warn"))
                    break

            if task_info.get("confirmed_offline"):
                break

        threading.Thread(
            target=merge_short_segments,
            args=(task_info.get("session_segments", []), task_info["log_prefix"]),
            daemon=True
        ).start()

        if not is_shutting_down:
            root.after(1000, lambda tid=task_id: remove_task_ui(tid))
            if username and username.lower() in monitored_targets_data:
                root.after(1500, lambda u=username: add_inactive_target_ui(u))

    threading.Thread(target=run, daemon=True).start()


def start_auto_monitor(username, target_config=None, open_settings_if_new=False):
    global task_counter

    if is_shutting_down:
        return

    key = username.lower()
    is_new = key not in monitored_targets_data

    for tid, task in list(active_tasks.items()):
        if task.get("username") and task["username"].lower() == key and task.get("is_auto_monitor"):
            if not task.get("proc"):
                task["user_stopped"] = True
                del active_tasks[tid]
                if task.get("row_frame"):
                    try:
                        task["row_frame"].destroy()
                    except Exception:
                        pass

    if target_config:
        monitored_targets_data[key] = target_config
    elif is_new:
        monitored_targets_data[key] = {
            "username": username,
            "mode": "automatic",
            "interval": poll_interval_seconds,
            "schedule_enabled": False,
            "schedules": [{"start_time": "05:00 PM", "end_time": "11:30 PM"}],
            "preferred_quality": GLOBAL_QUALITY_SETTINGS["preferred"],
            "allow_lower": GLOBAL_QUALITY_SETTINGS["allow_lower"],
            "transcribe": False
        }
    save_monitored_targets()

    if (is_new or open_settings_if_new) and target_config is None:
        root.after(100, lambda u=username: open_task_settings(u))

    cfg = monitored_targets_data.get(key, {})
    if cfg.get("mode") == "favorite":
        add_inactive_target_ui(username)
        return

    add_inactive_target_ui(username)

    for tid, task in list(active_tasks.items()):
        if task["username"] and task["username"].lower() == key:
            log(f"[Auto] Already monitoring or recording @{username} (Task {tid}).", "warn")
            return

    task_counter += 1
    task_id = f"task_{task_counter}"

    task_info = {
        "proc": None,
        "url": None,
        "username": username,
        "output_file": None,
        "user_stopped": False,
        "stop_reason": None,
        "session_segments": [],
        "confirmed_offline": False,
        "status_var": tk.StringVar(value="[AUTOMATIC] Monitoring (Offline)..."),
        "sub_status_var": tk.StringVar(value="🔍 Checking Stream..."),
        "row_frame": None,
        "stop_btn": None,
        "log_prefix": f"[@{username}] ",
        "start_time": None,
        "needs_new_url": False,
        "transcript_seg_dir": None,
        "transcript_file": None,
        "dead_stream_ids": set(),
        "is_auto_monitor": True
    }

    active_tasks[task_id] = task_info

    target_cfg_for_transcribe = monitored_targets_data.get(key, {})
    transcribe_this_task = transcribe_active and target_cfg_for_transcribe.get("transcribe", False)
    seg_dir = None
    if transcribe_this_task:
        seg_dir = os.path.join(TRANSCRIPT_DIR, task_id)
        os.makedirs(seg_dir, exist_ok=True)
        task_info["transcript_seg_dir"] = seg_dir
        safe_name = re.sub(r'[^A-Za-z0-9_-]', '_', username or "auto")
        task_info["transcript_file"] = os.path.join(OUTPUT_DIR, f"{safe_name}_{task_id}_transcript.txt")
        threading.Thread(target=watch_transcript_segments, args=(task_id,), daemon=True).start()
        root.after(0, lambda: log(f"{task_info['log_prefix']}Live transcription enabled (~{TRANSCRIBE_CHUNK_SECONDS}s updates) -> {task_info['transcript_file']}", "good"))

    def run_auto_monitor():
        while not task_info["user_stopped"] and not is_shutting_down:
            cfg_now = monitored_targets_data.get(key, {})
            if cfg_now.get("mode") == "favorite":
                break

            custom_interval = cfg_now.get("interval", poll_interval_seconds)

            if not is_target_in_active_schedule(cfg_now):
                update_inactive_target_status(username, "[AUTOMATIC] Scheduled (Paused)")
                set_task_sub_status(task_id, "Scheduled (Paused)")
                time.sleep(10)
                continue

            time.sleep(0.2 * (int(task_id.replace("task_", "")) % 5))

            # If this username is already actively recording under some other
            # task, there's nothing useful a live-status poll can do - skip
            # the network fetch (and any bot-check/challenge it could
            # trigger) entirely rather than hitting TikTok for a URL nobody
            # needs while a good recording is already running.
            already_recording_elsewhere = any(
                other_tid != task_id
                and other_task.get("username")
                and other_task["username"].lower() == key
                and other_task.get("proc") is not None
                and other_task["proc"].poll() is None
                for other_tid, other_task in list(active_tasks.items())
            )
            if already_recording_elsewhere:
                update_inactive_target_status(username, "[AUTOMATIC] Already recording (other task)")
                set_task_sub_status(task_id, "🔴 Recording (other task)")
                time.sleep(custom_interval)
                continue

            update_inactive_target_status(username, "[AUTOMATIC] Checking live status...")
            set_task_sub_status(task_id, "🔍 Checking Stream...")

            pref_rank = QUALITY_RANK.get(cfg_now.get("preferred_quality", GLOBAL_QUALITY_SETTINGS["preferred"]), QUALITY_RANK["origin"])
            allow_lower = cfg_now.get("allow_lower", GLOBAL_QUALITY_SETTINGS["allow_lower"])
            with http_request_lock:
                url, detail = fetch_stream_url_http(username, pref_rank, allow_lower)

            if detail == CHALLENGE_PAGE_DETAIL and not is_shutting_down:
                # Background/automated polling has no human watching it, so it
                # opens the solve browser itself with no confirmation prompt -
                # gated by a global cooldown (not per-target) so at most one
                # unattended browser opens across ALL targets every
                # AUTO_CHALLENGE_COOLDOWN seconds, and only one is ever open
                # at a time (enforced inside run_manual_challenge_browser).
                # If the bot-check keeps recurring faster than that cadence,
                # silent auto-retry has stopped being enough - fall back to
                # the "Verification Required" dialog instead of doing nothing.
                global _last_auto_challenge_attempt
                now = time.time()
                if _challenge_browser_active_user is not None:
                    update_inactive_target_status(username, "[AUTOMATIC] Bot-check - waiting on browser (busy)")
                    set_task_sub_status(task_id, "⚠ Waiting on browser")
                elif (now - _last_auto_challenge_attempt) > AUTO_CHALLENGE_COOLDOWN:
                    _last_auto_challenge_attempt = now
                    root.after(0, lambda u=username: log(
                        f"[Auto] @{u}: bot-check page detected during background poll - "
                        f"opening a browser window to refresh the session automatically.", "warn"
                    ))
                    root.after(0, lambda u=username, pr=pref_rank, al=allow_lower: run_manual_challenge_browser(u, pr, al))
                    update_inactive_target_status(username, "[AUTOMATIC] Bot-check - browser solving...")
                    set_task_sub_status(task_id, "⚠ Verification needed")
                else:
                    root.after(0, lambda u=username: log(
                        f"[Auto] @{u}: bot-check recurring faster than the "
                        f"{AUTO_CHALLENGE_COOLDOWN}s auto-retry window - asking for a manual solve.", "warn"
                    ))
                    root.after(0, lambda u=username, pr=pref_rank, al=allow_lower: prompt_manual_challenge_browser(u, pr, al))
                    update_inactive_target_status(username, "[AUTOMATIC] Bot-check - verification needed")
                    set_task_sub_status(task_id, "⚠ Verification needed")

            if url and not is_shutting_down:
                duplicate_active = any(
                    other_tid != task_id
                    and other_task.get("username")
                    and other_task["username"].lower() == key
                    and other_task.get("proc") is not None
                    and other_task["proc"].poll() is None
                    for other_tid, other_task in list(active_tasks.items())
                )
                if duplicate_active:
                    root.after(0, lambda u=username: log(f"[Auto] @{u} is already being recorded by another task - skipping duplicate start.", "warn"))
                    update_inactive_target_status(username, "[AUTOMATIC] Monitoring (Offline)...")
                    set_task_sub_status(task_id, "")
                    time.sleep(custom_interval)
                    continue

                task_info["url"] = url
                root.after(0, lambda u=username: log(f"[Auto] @{u} is LIVE! Moving to Active Recordings...", "good"))
                
                remove_inactive_target_ui(username)
                root.after(0, lambda tid=task_id: create_task_row(tid))

                output_file = get_output_path(username, task_id)
                task_info["output_file"] = output_file

                cmd = [
                    FFMPEG,
                    "-referer", "https://www.tiktok.com/",
                    "-user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
                    "-reconnect", "1",
                    "-reconnect_at_eof", "1",
                    "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "30",
                    "-i", task_info["url"],
                    "-c", "copy",
                    output_file
                ]

                if transcribe_this_task:
                    cmd.extend([
                        "-vn", "-ac", "1", "-ar", "16000",
                        "-f", "segment", "-segment_time", str(TRANSCRIBE_CHUNK_SECONDS), "-reset_timestamps", "1",
                        os.path.join(seg_dir, "chunk_%05d.wav")
                    ])

                recent_lines = []
                try:
                    proc = subprocess.Popen(
                        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, universal_newlines=True, bufsize=1,
                        creationflags=subprocess.CREATE_NO_WINDOW
                    )
                    task_info["proc"] = proc
                    task_info["start_time"] = time.time()
                    set_task_sub_status(task_id, "")  # Blank while actively recording
                except Exception as e:
                    log(f"Failed to start FFMPEG for @{username}: {e}", "warn")
                    time.sleep(5)
                    continue

                log(f"{task_info['log_prefix']}Recording started -> {output_file}", "good")
                log(f"{task_info['log_prefix']}Quality: {get_quality_label(task_info['url'])}", "good")
                log(f"{task_info['log_prefix']}Active Stream URL: {task_info['url']}", "normal")

                for line in proc.stdout:
                    line = line.rstrip()
                    recent_lines.append(line)
                    if len(recent_lines) > 10:
                        recent_lines.pop(0)

                    full_debug_log_buffer.append(f"[{task_info['log_prefix']}] {line}")

                    if "time=" in line and "bitrate=" in line:
                        match = re.search(r"time=(\S+)", line)
                        if match:
                            t_str = match.group(1)
                            root.after(0, lambda tid=task_id, t=t_str: active_tasks.get(tid, {}).get("status_var", tk.StringVar()).set(f"[AUTOMATIC] Recording  |  {t}"))

                proc.wait()
                exit_code = proc.returncode
                task_info["proc"] = None

                log_stream_summary(task_info, exit_code, recent_lines)

                segment_was_clean = exit_code in (0, 255, 1, -1)
                if segment_was_clean:
                    root.after(0, lambda pref=task_info['log_prefix']: log(f"{pref}Segment closed. Remuxing to MP4...", "normal"))
                else:
                    root.after(0, lambda pref=task_info['log_prefix'], ec=exit_code: log(f"{pref}ffmpeg exited with code {ec} - attempting error-tolerant remux instead of discarding...", "warn"))
                
                set_task_sub_status(task_id, "⚙ Remuxing to MP4...")
                threading.Thread(
                    target=remux_to_mp4,
                    args=(output_file, task_id),
                    kwargs={"error_tolerant": not segment_was_clean},
                    daemon=True
                ).start()

                task_info.setdefault("session_segments", []).append({
                    "flv_path": output_file,
                    "mp4_path": output_file.replace(".flv", ".mp4"),
                    "start": task_info.get("start_time", time.time()),
                    "end": time.time(),
                })

                if not is_shutting_down:
                    root.after(0, lambda tid=task_id: remove_task_ui(tid))
                    root.after(500, lambda u=username: add_inactive_target_ui(u))

                if task_info["user_stopped"] or is_shutting_down:
                    break

                root.after(0, lambda u=username: log(f"[Auto] Stream for @{u} ended. Resuming background monitoring...", "normal"))

            for remaining in range(custom_interval, 0, -1):
                if task_info["user_stopped"] or is_shutting_down:
                    break
                cfg_check = monitored_targets_data.get(key, {})
                if not is_target_in_active_schedule(cfg_check):
                    update_inactive_target_status(username, "[AUTOMATIC] Scheduled (Paused)")
                    set_task_sub_status(task_id, "Scheduled (Paused)")
                    break
                update_inactive_target_status(username, f"[AUTOMATIC] Offline (Check in {remaining}s)")
                set_task_sub_status(task_id, f"✔ Offline ({remaining}s)")
                time.sleep(1)

        threading.Thread(
            target=merge_short_segments,
            args=(task_info.get("session_segments", []), task_info["log_prefix"]),
            daemon=True
        ).start()

        if not is_shutting_down:
            root.after(0, lambda tid=task_id: remove_task_ui(tid))
            root.after(500, lambda u=username: add_inactive_target_ui(u))

    threading.Thread(target=run_auto_monitor, daemon=True).start()


def prompt_resume_dialog(username, task_info):
    if is_shutting_down:
        return "stop"
        
    dlg = tk.Toplevel(root)
    dlg.title("Resume Scheduled Monitoring?")
    dlg.configure(bg=SURFACE)
    dlg.geometry("460x220")
    dlg.transient(root)
    dlg.grab_set()
    dlg.resizable(False, False)

    choice = {"action": "stop"}

    tk.Label(
        dlg, text=f"Resume scheduled monitoring for @{username}?",
        font=("Segoe UI", 11, "bold"), fg=FG, bg=SURFACE, wrap=420, justify="center"
    ).pack(padx=20, pady=(22, 8))

    tk.Label(
        dlg, text="Recording has been stopped. Choose how automated monitoring should proceed:",
        font=FONT_LABEL, fg=FG_DIM, bg=SURFACE, wrap=420, justify="center"
    ).pack(padx=20, pady=(0, 20))

    btn_frame = tk.Frame(dlg, bg=SURFACE)
    btn_frame.pack(padx=16, pady=(0, 16))

    def on_yes():
        choice["action"] = "yes"
        dlg.destroy()

    def on_skip():
        choice["action"] = "skip"
        dlg.destroy()

    def on_stop():
        choice["action"] = "stop"
        dlg.destroy()

    btn_yes = tk.Button(
        btn_frame, text="Yes", font=FONT_BTN, bg=FG_GOOD, fg="#000000",
        activebackground="#2cb865", activeforeground="#000000",
        relief="flat", bd=0, padx=16, pady=8, cursor="hand2", command=on_yes
    )
    btn_yes.pack(side="left", padx=6)

    btn_skip = tk.Button(
        btn_frame, text="No, skip current schedule", font=FONT_BTN, bg="#333333", fg=FG_WARN,
        activebackground="#444444", activeforeground=FG_WARN,
        relief="flat", bd=0, padx=14, pady=8, cursor="hand2", command=on_skip
    )
    btn_skip.pack(side="left", padx=6)

    btn_stop = tk.Button(
        btn_frame, text="Stop", font=FONT_LABEL, bg=SURFACE, fg=FG_DIM,
        activebackground=SURFACE, activeforeground=FG,
        relief="flat", bd=0, padx=10, pady=8, cursor="hand2", command=on_stop
    )
    btn_stop.pack(side="left", padx=6)

    root.wait_window(dlg)
    return choice["action"]


def stop_task(task_id, reason="Stopped by user (stop button)", prompt_resume=True):
    if task_id not in active_tasks:
        return
    task = active_tasks[task_id]
    username = task.get("username") or "Manual"

    beep_event = task.get("giveup_beep_event")
    if beep_event:
        beep_event.set()

    proc = task.get("proc")
    if proc and proc.poll() is None:
        try:
            proc.stdin.write("q")
            proc.stdin.flush()
        except Exception:
            pass

        def wait_and_kill():
            try:
                proc.wait(timeout=5)
                log(f"{task['log_prefix']}ffmpeg stopped gracefully.", "normal")
            except subprocess.TimeoutExpired:
                log(f"{task['log_prefix']}ffmpeg didn't stop — force killing...", "warn")
                try:
                    proc.kill()
                except Exception:
                    pass
        threading.Thread(target=wait_and_kill, daemon=True).start()

    if task.get("is_auto_monitor") and prompt_resume and not is_shutting_down:
        action = prompt_resume_dialog(username, task)
        key = username.lower()
        if action == "yes":
            if key in monitored_targets_data:
                monitored_targets_data[key]["skip_until"] = 0
                save_monitored_targets()
            log(f"[Auto] Resumed automated monitoring for @{username}.", "good")
            return
        elif action == "skip":
            if key in monitored_targets_data:
                cfg = monitored_targets_data[key]
                end_ts = get_current_schedule_end_timestamp(cfg)
                if end_ts:
                    cfg["skip_until"] = end_ts
                    save_monitored_targets()
                    log(f"[Auto] Skipped current schedule for @{username}. Will resume next schedule window.", "warn")
                else:
                    log(f"[Auto] No active schedule window to skip for @{username}.", "normal")
            return

    task["stop_reason"] = reason
    task["user_stopped"] = True
    set_task_sub_status(task_id, "Stopped")

    if task.get("stop_btn"):
        try:
            task["stop_btn"].config(state="disabled", bg=BORDER, fg=FG_DIM)
        except tk.TclError:
            pass

    try:
        task["status_var"].set("Stopped")
    except tk.TclError:
        pass
    log(f"{task['log_prefix']}Stopped.", "normal")
    
    if username and username.lower() in monitored_targets_data and not is_shutting_down:
        root.after(1000, lambda u=username: add_inactive_target_ui(u))


def _parse_time_parts(time_str):
    try:
        dt = datetime.strptime((time_str or "").strip(), "%I:%M %p")
        return str(int(dt.strftime("%I"))), dt.strftime("%M"), dt.strftime("%p")
    except Exception:
        return "5", "00", "PM"


def build_time_picker(parent, bg, initial="05:00 PM"):
    h, m, p = _parse_time_parts(initial)

    frame = tk.Frame(parent, bg=bg)

    hour_var = tk.StringVar(value=h)
    minute_var = tk.StringVar(value=m)
    period_var = tk.StringVar(value=p)

    hour_values = [str(i) for i in range(1, 13)]
    minute_values = [f"{i:02d}" for i in range(0, 60, 5)]

    combo_hour = ttk.Combobox(frame, textvariable=hour_var, values=hour_values, width=3, state="readonly")
    combo_hour.pack(side="left")
    tk.Label(frame, text=":", font=FONT_MONO, fg=FG, bg=bg).pack(side="left", padx=1)
    combo_minute = ttk.Combobox(frame, textvariable=minute_var, values=minute_values, width=3, state="readonly")
    combo_minute.pack(side="left")
    combo_period = ttk.Combobox(frame, textvariable=period_var, values=["AM", "PM"], width=4, state="readonly")
    combo_period.pack(side="left", padx=(4, 0))

    def get_value():
        return f"{int(hour_var.get()):02d}:{minute_var.get()} {period_var.get()}"

    return frame, get_value


def open_task_settings(target_identifier):
    username = target_identifier
    if target_identifier in active_tasks:
        username = active_tasks[target_identifier].get("username", target_identifier)

    key = username.lower()
    cfg = monitored_targets_data.get(key, {
        "username": username,
        "mode": "automatic",
        "interval": poll_interval_seconds,
        "schedule_enabled": False,
        "schedules": [{"start_time": "05:00 PM", "end_time": "11:30 PM"}],
        "preferred_quality": GLOBAL_QUALITY_SETTINGS["preferred"],
        "allow_lower": GLOBAL_QUALITY_SETTINGS["allow_lower"],
        "transcribe": False
    })

    dlg = tk.Toplevel(root)
    dlg.title(f"Settings - @{username}")
    dlg.geometry("520x500")
    dlg.configure(bg=SURFACE)
    dlg.transient(root)
    dlg.grab_set()

    tk.Label(dlg, text=f"Target Settings: @{username}", font=FONT_TITLE, fg=FG, bg=SURFACE).pack(anchor="w", padx=16, pady=(14, 8))

    frame_mode = tk.Frame(dlg, bg=SURFACE)
    frame_mode.pack(fill="x", padx=16, pady=3)
    tk.Label(frame_mode, text="Target Mode:", font=FONT_LABEL, fg=FG_DIM, bg=SURFACE).pack(side="left")
    mode_var = tk.StringVar(value=cfg.get("mode", "automatic"))
    combo_mode = ttk.Combobox(frame_mode, textvariable=mode_var, values=["automatic", "favorite"], width=14, state="readonly")
    combo_mode.pack(side="right")

    frame_interval = tk.Frame(dlg, bg=SURFACE)
    frame_interval.pack(fill="x", padx=16, pady=3)
    tk.Label(frame_interval, text="Recheck Interval:", font=FONT_LABEL, fg=FG_DIM, bg=SURFACE).pack(side="left")
    interval_var = tk.StringVar(value=f"{cfg.get('interval', 30)}s")
    combo_interval = ttk.Combobox(frame_interval, textvariable=interval_var, values=["15s", "30s", "60s", "120s", "300s"], width=10, state="readonly")
    combo_interval.pack(side="right")

    frame_quality = tk.Frame(dlg, bg=SURFACE)
    frame_quality.pack(fill="x", padx=16, pady=3)
    tk.Label(frame_quality, text="Quality:", font=FONT_LABEL, fg=FG_DIM, bg=SURFACE).pack(side="left")
    task_quality_var = tk.StringVar(value=QUALITY_LABEL.get(cfg.get("preferred_quality", "origin"), "Best (Origin)"))
    combo_quality = ttk.Combobox(frame_quality, textvariable=task_quality_var, values=QUALITY_LABELS_ORDERED, width=14, state="readonly")
    combo_quality.pack(side="right")

    task_allow_lower_var = tk.BooleanVar(value=cfg.get("allow_lower", False))
    cb_allow_lower = tk.Checkbutton(
        dlg, text="Fall back to lower quality if needed", variable=task_allow_lower_var,
        font=FONT_LABEL, fg=FG, bg=SURFACE, selectcolor="#333333",
        activebackground=SURFACE, activeforeground=FG
    )
    cb_allow_lower.pack(anchor="w", padx=16, pady=(2, 6))

    task_transcribe_var = tk.BooleanVar(value=cfg.get("transcribe", False))
    cb_transcribe = tk.Checkbutton(
        dlg, text="Live transcription (Whisper)", variable=task_transcribe_var,
        font=FONT_LABEL, fg=FG, bg=SURFACE, selectcolor="#333333",
        activebackground=SURFACE, activeforeground=FG
    )
    cb_transcribe.pack(anchor="w", padx=16, pady=(0, 6))

    panel_sched = tk.Frame(dlg, bg="#1a1a1a", bd=1, highlightthickness=1, highlightbackground=BORDER)
    panel_sched.pack(fill="x", padx=16, pady=6)

    header_sched = tk.Frame(panel_sched, bg="#1a1a1a")
    header_sched.pack(fill="x", padx=12, pady=8)

    tk.Label(header_sched, text="SCHEDULER", font=("Segoe UI", 10, "bold"), fg=FG, bg="#1a1a1a").pack(side="left")

    sched_enabled_var = tk.BooleanVar(value=cfg.get("schedule_enabled", False))

    def update_sched_btn_style():
        if sched_enabled_var.get():
            btn_sched_toggle.config(text="ON", bg=FG_GOOD, fg="#000000", activebackground="#2cb865", activeforeground="#000000")
        else:
            btn_sched_toggle.config(text="OFF", bg="#cc0033", fg=FG, activebackground="#990022", activeforeground=FG)

    def toggle_sched():
        new_state = not sched_enabled_var.get()
        sched_enabled_var.set(new_state)
        update_sched_btn_style()
        if new_state:
            mode_var.set("automatic")

    btn_sched_toggle = tk.Button(
        header_sched, font=("Segoe UI", 9, "bold"), relief="flat", bd=0, padx=12, pady=2, cursor="hand2",
        command=toggle_sched
    )
    btn_sched_toggle.pack(side="left", padx=12)
    update_sched_btn_style()

    schedules_container = tk.Frame(panel_sched, bg="#1a1a1a")
    schedules_container.pack(fill="x", padx=12, pady=(0, 8))

    sched_rows = []

    def add_schedule_row(start_val="05:00 PM", end_val="11:30 PM"):
        row_frame = tk.Frame(schedules_container, bg="#1a1a1a")
        row_frame.pack(fill="x", pady=3)

        tk.Label(row_frame, text="Start:", font=FONT_LABEL, fg=FG_DIM, bg="#1a1a1a").pack(side="left")
        picker_start, get_start = build_time_picker(row_frame, "#1a1a1a", start_val)
        picker_start.pack(side="left", padx=(4, 12))

        tk.Label(row_frame, text="End:", font=FONT_LABEL, fg=FG_DIM, bg="#1a1a1a").pack(side="left")
        picker_end, get_end = build_time_picker(row_frame, "#1a1a1a", end_val)
        picker_end.pack(side="left", padx=(4, 8))

        row_item = {"frame": row_frame, "get_start": get_start, "get_end": get_end}

        def remove_row():
            row_frame.destroy()
            if row_item in sched_rows:
                sched_rows.remove(row_item)

        btn_del = tk.Button(
            row_frame, text="✕", font=("Segoe UI", 10, "bold"), bg="#1a1a1a", fg="#ff5555",
            activebackground="#1a1a1a", activeforeground="#ff2222", relief="flat", bd=0,
            padx=6, cursor="hand2", command=remove_row
        )
        btn_del.pack(side="right", padx=(4, 0))

        sched_rows.append(row_item)

    btn_add_sched = tk.Button(
        header_sched, text="+", font=("Segoe UI", 11, "bold"), bg="#333333", fg=FG,
        activebackground="#444444", activeforeground=FG, relief="flat", bd=0, padx=8, pady=0, cursor="hand2",
        command=lambda: add_schedule_row()
    )
    btn_add_sched.pack(side="right")

    raw_schedules = cfg.get("schedules")
    if not raw_schedules:
        raw_schedules = [{"start_time": cfg.get("start_time", "05:00 PM"), "end_time": cfg.get("end_time", "11:30 PM")}]

    for s_item in raw_schedules:
        add_schedule_row(s_item.get("start_time", "05:00 PM"), s_item.get("end_time", "11:30 PM"))

    def save_settings():
        raw_int = interval_var.get().replace("s", "")
        try:
            val_int = int(raw_int)
        except ValueError:
            val_int = 30

        compiled_schedules = [
            {"start_time": item["get_start"](), "end_time": item["get_end"]()}
            for item in sched_rows
        ]
        if not compiled_schedules:
            compiled_schedules = [{"start_time": "05:00 PM", "end_time": "11:30 PM"}]

        new_mode = mode_var.get()
        monitored_targets_data[key] = {
            "username": username,
            "mode": new_mode,
            "interval": val_int,
            "schedule_enabled": sched_enabled_var.get(),
            "schedules": compiled_schedules,
            "preferred_quality": QUALITY_LABEL_TO_KEY.get(task_quality_var.get(), "origin"),
            "allow_lower": task_allow_lower_var.get(),
            "transcribe": task_transcribe_var.get()
        }
        save_monitored_targets()
        log(f"[Settings] Saved settings for @{username} (Mode: {new_mode}).", "good")

        if new_mode == "automatic":
            start_auto_monitor(username, monitored_targets_data[key])
        else:
            for tid, t in list(active_tasks.items()):
                if t.get("username") and t["username"].lower() == key and not t.get("proc"):
                    stop_task(tid, reason="Stopped: mode changed to Favorite", prompt_resume=False)
            add_inactive_target_ui(username)
        refresh_inactive_target_tag(username)

        dlg.destroy()

    def delete_automation():
        if messagebox.askyesno("Delete Target", f"Remove @{username} from saved targets?"):
            monitored_targets_data.pop(key, None)
            save_monitored_targets()
            remove_inactive_target_ui(username)
            for tid, t in list(active_tasks.items()):
                if t.get("username") and t["username"].lower() == key:
                    stop_task(tid, reason="Stopped: target deleted", prompt_resume=False)
            log(f"[Settings] Deleted target @{username}.", "warn")
            dlg.destroy()

    btn_frame = tk.Frame(dlg, bg=SURFACE)
    btn_frame.pack(fill="x", padx=16, pady=12)

    tk.Button(btn_frame, text="Save Settings", font=FONT_BTN, bg=FG_GOOD, fg="#000", relief="flat", padx=12, pady=6, cursor="hand2", command=save_settings).pack(side="left")
    tk.Button(btn_frame, text="Delete Target", font=FONT_LABEL, bg="#cc0033", fg=FG, relief="flat", padx=12, pady=6, cursor="hand2", command=delete_automation).pack(side="right")


def create_task_row(task_id):
    if task_id not in active_tasks or is_shutting_down:
        return
    task = active_tasks[task_id]

    row = tk.Frame(active_list_frame, bg="#222222", bd=0, highlightthickness=1, highlightbackground=BORDER)
    row.pack(fill="x", pady=4, padx=5)
    task["row_frame"] = row

    lbl_user = tk.Label(row, text=f"@{task['username'] or 'Manual'}", font=("Segoe UI", 10, "bold"), fg=FG, bg="#222222", width=16, anchor="w")
    lbl_user.pack(side="left", padx=12, pady=8)

    lbl_status = tk.Label(row, textvariable=task["status_var"], font=FONT_MONO, fg=FG_GOOD, bg="#222222", anchor="w")
    lbl_status.pack(side="left", fill="x", expand=True, padx=10)

    # Amber Live Status Indicator
    lbl_sub_status = tk.Label(row, textvariable=task["sub_status_var"], font=("Segoe UI", 9, "bold"), fg="#ffcc00", bg="#222222", anchor="e")
    lbl_sub_status.pack(side="left", padx=10)

    # Settings Cog Button
    btn_cog = tk.Button(
        row, text="⚙", font=FONT_LABEL, bg="#333333", fg=FG,
        activebackground="#444444", activeforeground=FG,
        relief="flat", bd=0, padx=8, pady=3, cursor="hand2",
        command=lambda: open_task_settings(task_id)
    )
    btn_cog.pack(side="right", padx=(0, 6))

    # Red Stop Button matching Check Live styling
    btn_stop = tk.Button(
        row, text="Stop", font=FONT_LABEL, bg="#cc0033", fg="#ffffff",
        activebackground="#990022", activeforeground="#ffffff",
        relief="flat", bd=0, padx=10, pady=3, cursor="hand2",
        command=lambda: stop_task(task_id)
    )
    btn_stop.pack(side="right", padx=6)
    task["stop_btn"] = btn_stop

    update_global_status()


def remove_task_ui(task_id):
    if task_id in active_tasks:
        task = active_tasks[task_id]
        if task["row_frame"]:
            try:
                task["row_frame"].destroy()
            except Exception:
                pass
        del active_tasks[task_id]
        update_global_status()


# ── Inactive / Saved Targets UI Section ──

def _natural_sort_key(s):
    return [int(chunk) if chunk.isdigit() else chunk.lower() for chunk in re.split(r'(\d+)', s)]


def resort_inactive_targets_ui():
    def sort_key(item):
        key, entry = item
        cfg = monitored_targets_data.get(key, {})
        is_fav = cfg.get("mode") == "favorite"
        return (1 if is_fav else 0, _natural_sort_key(entry.get("username", key)))

    for key, entry in sorted(inactive_targets_ui.items(), key=sort_key):
        try:
            entry["frame"].pack_forget()
            entry["frame"].pack(fill="x", pady=3, padx=5)
        except Exception:
            pass


def add_inactive_target_ui(username):
    if is_shutting_down:
        return
    key = username.lower()
    if key in inactive_targets_ui:
        return

    cfg = monitored_targets_data.get(key, {})
    is_fav = cfg.get("mode") == "favorite"
    tag_prefix = "[⭐ FAVORITE]" if is_fav else "[AUTOMATIC]"

    row = tk.Frame(inactive_list_frame, bg="#222222", bd=0, highlightthickness=1, highlightbackground=BORDER)
    row.pack(fill="x", pady=3, padx=5)

    lbl_user = tk.Label(row, text=f"@{username}", font=("Segoe UI", 10, "bold"), fg=FG_DIM, bg="#222222", width=18, anchor="w")
    lbl_user.pack(side="left", padx=12, pady=6)

    initial_status = f"{tag_prefix} Manual Only (Offline)" if is_fav else f"{tag_prefix} Offline"
    lbl_status = tk.Label(row, text=initial_status, font=FONT_MONO, fg=FG_DIM, bg="#222222", anchor="w")
    lbl_status.pack(side="left", fill="x", expand=True, padx=10)

    btn_start = tk.Button(
        row, text="Check Live", font=FONT_LABEL, bg=FG_GOOD, fg="#000000",
        activebackground="#2cb865", activeforeground="#000000",
        relief="flat", bd=0, padx=10, pady=3, cursor="hand2",
        command=lambda: check_live_now(username)
    )
    btn_start.pack(side="right", padx=6)

    btn_cog = tk.Button(
        row, text="⚙", font=FONT_LABEL, bg="#333333", fg=FG,
        activebackground="#444444", activeforeground=FG,
        relief="flat", bd=0, padx=8, pady=3, cursor="hand2",
        command=lambda: open_task_settings(username)
    )
    btn_cog.pack(side="right", padx=(0, 6))

    inactive_targets_ui[key] = {"frame": row, "status_label": lbl_status, "username": username, "status_gen": 0}
    update_inactive_empty_lbl()
    resort_inactive_targets_ui()


def _default_inactive_status_text(username):
    key = username.lower()
    cfg = monitored_targets_data.get(key, {})
    is_fav = cfg.get("mode") == "favorite"
    tag_prefix = "[⭐ FAVORITE]" if is_fav else "[AUTOMATIC]"
    return f"{tag_prefix} Manual Only (Offline)" if is_fav else f"{tag_prefix} Offline"


def update_inactive_target_status(username, status_text, revert_after=None):
    """Sets the status line for a target in Inactive Targets. If revert_after
    (seconds) is given, the line reverts to the normal default status
    (e.g. the [⭐ FAVORITE] Manual Only (Offline) line) after that delay -
    used for one-off Check Live results, which otherwise have no follow-up
    poll to naturally refresh the line back. A generation counter guards
    against the revert stomping on a newer status set in the meantime."""
    key = username.lower()
    if key not in inactive_targets_ui:
        return
    entry = inactive_targets_ui[key]
    entry["status_gen"] = entry.get("status_gen", 0) + 1
    my_gen = entry["status_gen"]

    def _do_update():
        try:
            lbl = entry.get("status_label")
            if lbl and lbl.winfo_exists():
                lbl.config(text=status_text)
        except Exception:
            pass
    root.after(0, _do_update)

    if revert_after is not None:
        def _do_revert():
            try:
                if inactive_targets_ui.get(key) is not entry:
                    return  # row was rebuilt/removed
                if entry.get("status_gen") != my_gen:
                    return  # something newer already updated this status
                lbl = entry.get("status_label")
                if lbl and lbl.winfo_exists():
                    lbl.config(text=_default_inactive_status_text(username))
            except Exception:
                pass
        root.after(int(revert_after * 1000), _do_revert)


def remove_inactive_target_ui(username):
    key = username.lower()
    if key in inactive_targets_ui:
        try:
            inactive_targets_ui[key]["frame"].destroy()
        except Exception:
            pass
        del inactive_targets_ui[key]
        update_inactive_empty_lbl()


def refresh_inactive_target_tag(username):
    key = username.lower()
    if key not in inactive_targets_ui:
        return
    cfg = monitored_targets_data.get(key, {})
    is_fav = cfg.get("mode") == "favorite"
    tag_prefix = "[⭐ FAVORITE]" if is_fav else "[AUTOMATIC]"
    lbl = inactive_targets_ui[key]["status_label"]
    try:
        current = lbl.cget("text")
        rest = current.split("]", 1)[-1].strip() if "]" in current else current
        lbl.config(text=f"{tag_prefix} {rest}")
        resort_inactive_targets_ui()
    except Exception:
        pass


def update_inactive_empty_lbl():
    try:
        if len(inactive_targets_ui) == 0:
            inactive_empty_lbl.pack(fill="x", pady=6)
        else:
            inactive_empty_lbl.pack_forget()
    except Exception:
        pass


def check_live_now(username):
    if is_shutting_down:
        return
    def worker():
        log(f"[Fetch] Manual check requested for @{username}...", "normal")
        pref_rank, allow_lower = get_effective_quality_settings(username)
        with http_request_lock:
            url, detail = fetch_stream_url_http(username, pref_rank, allow_lower)
        if url:
            log(f"[Fetch] @{username} is LIVE! Starting recording...", "good")
            start_recording(url=url, username=username)
        elif detail == CHALLENGE_PAGE_DETAIL:
            log(f"[Fetch] @{username}: {detail} - opening a browser to solve it automatically.", "warn")
            root.after(0, lambda: run_manual_challenge_browser(username, pref_rank, allow_lower))
        else:
            log(f"[Fetch] @{username} is currently offline ({detail}).", "normal")
            update_inactive_target_status(username, f"Offline ({detail})", revert_after=10)
    threading.Thread(target=worker, daemon=True).start()


def update_global_status():
    try:
        count = len(active_tasks)
        if count == 0:
            empty_lbl.pack(fill="x", pady=10)
        else:
            empty_lbl.pack_forget()
    except Exception:
        pass


def open_recordings():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.startfile(OUTPUT_DIR)


def clear_log():
    log_box.config(state="normal")
    log_box.delete("1.0", tk.END)
    log_box.config(state="disabled")


def toggle_debug_log():
    global debug_visible
    geom = root.geometry()
    m = re.match(r"^(\d+)x(\d+)(.*)$", geom)
    w = int(m.group(1)) if m else 700
    h = int(m.group(2)) if m else 680
    rest = m.group(3) if m else ""

    if debug_visible:
        log_frame.pack_forget()
        btn_debug.config(text="Show Debug Log ▾", fg=FG_DIM)
        debug_visible = False
        new_h = max(500, h - 220)
        root.geometry(f"{w}x{new_h}{rest}")
    else:
        log_frame.pack(fill="both", expand=True, padx=20, pady=(0, 16))
        btn_debug.config(text="Hide Debug Log ▴", fg=FG_GOOD)
        debug_visible = True
        new_h = h + 220
        root.geometry(f"{w}x{new_h}{rest}")


def save_logs_on_exit():
    try:
        os.makedirs(RECORDER_LOG_DIR, exist_ok=True)
        full_text = log_box.get("1.0", tk.END)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"recorder_shutdown_logs_{timestamp}_{uuid.uuid4().hex[:8]}.txt"
        filepath = os.path.join(RECORDER_LOG_DIR, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("=== MAIN UI LOG SUMMARY ===\n\n")
            f.write(full_text)
            f.write("\n\n=== FULL UNFILTERED TECHNICAL DEBUG LOG (FFMPEG STDOUT & BUFFER) ===\n\n")
            f.write("\n".join(full_debug_log_buffer))
    except Exception as e:
        try:
            print(f"[SHUTDOWN] Failed to save recorder logs: {e}")
        except Exception:
            pass


# ── Shutdown Animated Overlay & Sequence ──────────────────────────────────────

class ShutdownOverlay:
    """Full-window modal overlay featuring a smooth spinning arc animation during system exit."""
    def __init__(self, parent):
        self.parent = parent
        self.overlay = tk.Frame(parent, bg="#111111")
        self.overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.overlay.lift()

        self.center_frame = tk.Frame(self.overlay, bg="#111111")
        self.center_frame.place(relx=0.5, rely=0.5, anchor="center")

        self.canvas = tk.Canvas(self.center_frame, width=100, height=100, bg="#111111", highlightthickness=0)
        self.canvas.pack(pady=(0, 16))

        self.lbl_title = tk.Label(self.center_frame, text="SHUTTING DOWN", font=("Segoe UI", 16, "bold"), fg="#ff2d55", bg="#111111")
        self.lbl_title.pack(pady=(0, 6))

        self.lbl_status = tk.Label(self.center_frame, text="Stopping active streams & exporting MP4s...", font=("Segoe UI", 10), fg="#aaaaaa", bg="#111111")
        self.lbl_status.pack()

        self.angle = 0
        self.running = True
        self.animate()

    def animate(self):
        if not self.running:
            return
        try:
            self.canvas.delete("all")
            # Background ring
            self.canvas.create_oval(12, 12, 88, 88, outline="#222222", width=6)
            # Glowing spinning arc
            self.canvas.create_arc(12, 12, 88, 88, start=self.angle, extent=100, outline="#ff2d55", style="arc", width=6)
            self.angle = (self.angle + 12) % 360
            self.parent.after(30, self.animate)
        except Exception:
            pass

    def update_status(self, text):
        try:
            self.lbl_status.config(text=text)
        except Exception:
            pass

    def stop(self):
        self.running = False


def initiate_shutdown():
    """Triggers the full shutdown sequence: shows overlay, stops streams, waits for exports, saves logs, exits."""
    global is_shutting_down
    if is_shutting_down:
        return
    is_shutting_down = True

    log("[Shutdown] System shutdown initiated by user...", "warn")

    # Display modal animated overlay
    overlay = ShutdownOverlay(root)

    # Stop all active tasks
    for tid, task in list(active_tasks.items()):
        if not task.get("user_stopped"):
            stop_task(tid, reason="Stopped: System Shutdown Initiated", prompt_resume=False)

    def shutdown_poll():
        # Active recording processes
        live_procs = [
            tid for tid, t in list(active_tasks.items())
            if t.get("proc") and t["proc"].poll() is None
        ]

        # Active exports (remuxing / merging)
        with export_lock:
            num_exports = len(active_exports)

        if live_procs:
            overlay.update_status(f"Stopping {len(live_procs)} active recording(s)...")
            root.after(200, shutdown_poll)
        elif num_exports > 0:
            overlay.update_status(f"Exporting & remuxing {num_exports} video file(s) to MP4...")
            root.after(200, shutdown_poll)
        else:
            overlay.update_status("Finalizing logs & exiting...")
            root.after(400, finalize_and_exit)

    def finalize_and_exit():
        try:
            save_logs_on_exit()
            overlay.stop()
            root.destroy()
        except Exception:
            os._exit(0)

    root.after(100, shutdown_poll)


# ── HTTP Stream URL Utilities ──────────────────────────────────────────────

_cookie_cache = {"mtime": None, "jar": None}


def get_poller_cookie_jar():
    global _cookie_cache
    try:
        mtime = os.path.getmtime(COOKIES_FILE)
    except OSError:
        return None
    if _cookie_cache["mtime"] == mtime:
        return _cookie_cache["jar"]
    try:
        with open(COOKIES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        jar = {}
        for entry in data:
            name = entry.get("name")
            value = entry.get("value")
            if name and value is not None:
                jar[name] = value
        _cookie_cache = {"mtime": mtime, "jar": jar}
        return jar
    except Exception as e:
        root.after(0, lambda err=e: log(f"[Fetcher] Failed to parse cookies.json: {e}", "warn"))
        return None


def normalize_username(raw):
    raw = (raw or "").strip()
    m = re.search(r"tiktok\.com/@([^/?#]+)", raw, re.IGNORECASE)
    if m:
        return m.group(1)
    if raw.startswith("@"):
        raw = raw[1:]
    return raw.strip()


HANDLE_PLACEHOLDER = "@handle"
HANDLE_MAX_LEN = 24
_formatting_handle = False


def on_handle_focus_in(event=None):
    if handle_var.get() == HANDLE_PLACEHOLDER:
        handle_var.set("@")
        handle_entry.config(fg=FG)
        handle_entry.icursor(tk.END)
    else:
        handle_entry.config(fg=FG)


def on_handle_focus_out(event=None):
    val = handle_var.get().strip()
    if not val or val == "@":
        handle_var.set(HANDLE_PLACEHOLDER)
        handle_entry.config(fg=FG_DIM)
    else:
        handle_entry.config(fg=FG)


def format_handle_input(event=None):
    global _formatting_handle
    if _formatting_handle:
        return

    val = handle_var.get()

    if val == HANDLE_PLACEHOLDER:
        handle_entry.config(fg=FG_DIM)
        return

    _formatting_handle = True

    handle_entry.config(fg=FG)

    m = re.search(r"tiktok\.com/@([^/?#]+)", val, re.IGNORECASE)
    if m:
        clean_handle = "@" + m.group(1)[:HANDLE_MAX_LEN]
        handle_var.set(clean_handle)
        handle_entry.icursor(tk.END)
        _formatting_handle = False
        return

    if val and not val.startswith("@"):
        curr_pos = handle_entry.index(tk.INSERT)
        new_val = "@" + val
        handle_var.set(new_val[:HANDLE_MAX_LEN + 1])
        handle_entry.icursor(curr_pos + 1)
    elif len(val) > HANDLE_MAX_LEN + 1:
        curr_pos = handle_entry.index(tk.INSERT)
        handle_var.set(val[:HANDLE_MAX_LEN + 1])
        handle_entry.icursor(min(curr_pos, HANDLE_MAX_LEN + 1))

    _formatting_handle = False


def on_handle_paste(event=None):
    try:
        pasted = root.clipboard_get().strip()
        m = re.search(r"tiktok\.com/@([^/?#]+)", pasted, re.IGNORECASE)
        if m:
            extracted = m.group(1)[:HANDLE_MAX_LEN]
            handle_var.set(f"@{extracted}")
            handle_entry.config(fg=FG)
            handle_entry.icursor(tk.END)
            return "break"
    except Exception:
        pass
    root.after_idle(format_handle_input)


def get_handle_entry_value():
    val = handle_var.get().strip()
    if val in ("", HANDLE_PLACEHOLDER, "@"):
        return ""
    return val


def clear_handle_entry():
    handle_var.set(HANDLE_PLACEHOLDER)
    handle_entry.config(fg=FG_DIM)


def _find_all_flv_urls(obj):
    if isinstance(obj, (dict, list)):
        raw_text = json.dumps(obj)
    elif isinstance(obj, str):
        raw_text = obj
    else:
        return []

    raw_text = raw_text.replace(r"\/", "/").replace(r"\u002F", "/")
    pattern = r'https?://[^\s"\'\\]*pull-[^\s"\'\\]*\.flv[^\s"\'\\]*'
    matches = re.findall(pattern, raw_text, re.IGNORECASE)
    return list(set(matches))


def _strip_audio_only(url):
    cleaned = re.sub(r"[?&]only_audio=1", "", url)
    if "?" not in cleaned and "&" in cleaned:
        cleaned = cleaned.replace("&", "?", 1)
    return cleaned


def _classify_stream_tier(url):
    try:
        path_part = url.split("?")[0].rsplit("/", 1)[-1]
        if not path_part.endswith(".flv"):
            return 99
        name = path_part[:-4]
        parts = name.split("-")
        if len(parts) <= 1:
            return 99
        id_part = parts[-1]

        if "_" not in id_part:
            return QUALITY_RANK["origin"]
        if "_uhd" in id_part:
            return QUALITY_RANK["uhd"]
        if "_zsd" in id_part:
            return QUALITY_RANK["zsd"]
        if "_ld" in id_part:
            return LD_RANK
        if "_sd" in id_part:
            return QUALITY_RANK["sd"]
        if "_hd" in id_part:
            return QUALITY_RANK["hd"]
        return 99
    except Exception:
        return 99


def check_stream_quality(url, floor_rank=None):
    if floor_rank is None:
        floor_rank = QUALITY_RANK["uhd"]
    rank = _classify_stream_tier(url)
    return rank <= floor_rank


def _select_flv_url(obj, preferred_rank, allow_below):
    all_raw = _find_all_flv_urls(obj)
    if not all_raw:
        return None

    cleaned_candidates = sorted(set(_strip_audio_only(u) for u in all_raw))

    by_rank = {}
    for url in cleaned_candidates:
        rank = _classify_stream_tier(url)
        if rank != 99 and rank not in by_rank:
            by_rank[rank] = url

    if not by_rank:
        return None

    if preferred_rank in by_rank:
        return by_rank[preferred_rank]

    better = [r for r in by_rank if r < preferred_rank]
    if better:
        return by_rank[max(better)]

    if allow_below:
        worse = [r for r in by_rank if r > preferred_rank]
        if worse:
            return by_rank[min(worse)]

    return None


def _extract_hydration_json(html, element_id):
    pattern = rf'<script[^>]*id="{re.escape(element_id)}"[^>]*>(.*?)</script>'
    match = re.search(pattern, html, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(1).strip())
    except json.JSONDecodeError:
        return None


def _resolve_live_url_from_hydration(data, element_id, preferred_rank, allow_below):
    """Given one parsed hydration JSON blob (SIGI_STATE or
    __UNIVERSAL_DATA_FOR_REHYDRATION__), decides whether the room is
    ACTUALLY live right now and, if so, picks a stream URL. Returns
    (url, detail). url is None if not live / nothing usable.

    Critical: TikTok embeds the room's last-known stream info in this
    blob even when the user is offline, complete with an already-expired
    signed FLV URL. Regexing for a "pull-...flv" pattern without checking
    the status field will happily hand back that stale URL and ffmpeg
    will immediately 404 on it. The status check below is what tells a
    genuinely live room apart from a cached-but-dead one - every caller
    that wants a stream URL out of this data MUST go through here rather
    than calling _find_all_flv_urls/_select_flv_url directly.
    """
    try:
        live_room = None
        if isinstance(data, dict):
            live_room = (data.get("LiveRoom", {})
                         .get("liveRoomUserInfo", {})
                         .get("liveRoom", {}))

        if live_room and "status" in live_room:
            room_status = live_room.get("status")
            if room_status == 4:
                return None, f"stream ended (status 4 in {element_id})"
            elif room_status != 2:
                return None, f"stream inactive (status {room_status} in {element_id})"
    except Exception:
        pass

    raw_str = json.dumps(data) if isinstance(data, (dict, list)) else str(data)
    if '"status":4' in raw_str or '"status": 4' in raw_str or '"status_code":4' in raw_str:
        return None, f"stream confirmed ended (status 4 in {element_id})"

    urls = _find_all_flv_urls(data)
    if not urls:
        return None, None  # caller should keep trying other element_ids

    url = _select_flv_url(data, preferred_rank, allow_below)
    if url:
        return url, element_id
    else:
        return None, f"live but no tier meeting the quality setting was offered ({element_id})"


def fetch_stream_url_http(username, preferred_rank=None, allow_below=False):
    if preferred_rank is None:
        preferred_rank = QUALITY_RANK["origin"]
    jar = get_poller_cookie_jar()

    target = f"https://www.tiktok.com/@{quote(username, safe='.')}/live"
    headers = {
        "User-Agent": HTTP_POLLER_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.tiktok.com/",
        "Sec-Ch-Ua": '"Chromium";v="131", "Not?A_Brand";v="8", "Google Chrome";v="131"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1"
    }

    if not CURL_CFFI_AVAILABLE:
        root.after(0, lambda: log(
            "[Fetch-Debug] curl_cffi is not installed - falling back to plain "
            "requests, which TikTok's WAF has been known to block. Run: "
            "pip install curl_cffi",
            "warn"
        ))
        try:
            resp = requests.get(target, headers=headers, cookies=jar, timeout=10)
        except Exception as e:
            return None, f"request failed: {e}"
    else:
        try:
            resp = cffi_requests.get(
                target, headers=headers, cookies=jar, timeout=10,
                impersonate=CURL_CFFI_IMPERSONATE
            )
        except Exception as e:
            return None, f"request failed (curl_cffi): {e}"

    body_len = len(resp.text or "")
    has_cookies = bool(jar)
    tag_hits = [eid for eid in HTTP_POLLER_HYDRATION_IDS if eid in resp.text]
    root.after(0, lambda: log(
        f"[Fetch-Debug] @{username}: HTTP {resp.status_code}, body={body_len} bytes, "
        f"cookies_loaded={has_cookies}, hydration_tags_present={tag_hits or 'NONE'}",
        "normal"
    ))

    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}"

    if not tag_hits:
        # Page loaded but contains neither hydration blob at all - this is the
        # tell for a stale/missing session or an anti-bot interstitial, not a
        # genuinely offline user. A real offline page still normally embeds
        # SIGI_STATE/__UNIVERSAL_DATA__ with a "not live" status inside it.
        snippet = re.sub(r"\s+", " ", (resp.text or "")[:300]).strip()
        root.after(0, lambda: log(
            f"[Fetch-Debug] @{username}: no hydration tags found in response. "
            f"First 300 chars: {snippet!r}",
            "warn"
        ))

    for element_id in HTTP_POLLER_HYDRATION_IDS:
        data = _extract_hydration_json(resp.text, element_id)
        if data is None:
            continue

        url, detail = _resolve_live_url_from_hydration(data, element_id, preferred_rank, allow_below)
        if url is not None:
            return url, detail
        if detail is not None:
            # A concrete negative result (ended/inactive/no matching tier) -
            # trust it and stop, same as before.
            return None, detail
        # detail is None: this element_id's blob had no FLV URLs at all,
        # keep trying the next hydration id.

    if not tag_hits:
        return None, CHALLENGE_PAGE_DETAIL
    return None, "stream offline or no stream URLs available"


# ── Manual Challenge-Solve Browser ─────────────────────────────────────────
# When TikTok serves a bot-check stub instead of the real page, no HTTP
# client can push past it - it requires a real browser actually running
# TikTok's JS, and sometimes an interactive challenge only a human can clear.
# This opens a real, visible browser window, waits for the person to solve
# whatever's shown (or does nothing if it clears on its own), then reads the
# resulting page + cookies back out and resumes the normal recording flow.

CHALLENGE_SOLVE_TIMEOUT = 240  # seconds to wait for the window to clear

# Global single-flight lock: only ONE challenge browser window is ever open at
# a time, across every target (manual or automated). Prevents multiple
# Playwright windows popping up at once if several targets go stale together.
_challenge_browser_lock = threading.Lock()
_challenge_browser_active_user = None  # username currently running the flow, or None

AUTO_CHALLENGE_COOLDOWN = 300  # automated/background pollers auto-open the solve browser (no confirmation) at most this often
_last_auto_challenge_attempt = 0  # time.time() of the last unattended auto-open, global (not per-target)
_challenge_prompt_pending = set()  # usernames with a "Verification Required" dialog already on screen, to avoid stacking dialogs


def save_cookies_from_playwright(cookie_list):
    """Writes cookies out in the same [{name, value}, ...] shape
    get_poller_cookie_jar() already knows how to read."""
    try:
        simplified = [{"name": c.get("name"), "value": c.get("value")} for c in cookie_list if c.get("name")]
        with open(COOKIES_FILE, "w", encoding="utf-8") as f:
            json.dump(simplified, f)
        return True
    except Exception as e:
        root.after(0, lambda: log(f"[Challenge] Failed to save cookies: {e}", "warn"))
        return False


def run_manual_challenge_browser(username, preferred_rank, allow_below):
    global _challenge_browser_active_user

    if not PLAYWRIGHT_AVAILABLE:
        root.after(0, lambda: log(
            "[Challenge] playwright is not installed. Run: pip install playwright "
            "&& playwright install chromium",
            "warn"
        ))
        return

    with _challenge_browser_lock:
        if _challenge_browser_active_user is not None:
            busy_user = _challenge_browser_active_user
            root.after(0, lambda: log(
                f"[Challenge] A browser window is already open for @{busy_user} - "
                f"@{username} will be retried on its next poll.", "normal"
            ))
            return
        _challenge_browser_active_user = username

    target = f"https://www.tiktok.com/@{quote(username, safe='.')}/live"
    root.after(0, lambda: log(f"[Challenge] Opening a browser window for @{username} - solve any prompt shown, "
                               f"the app will continue automatically once the page loads.", "good"))

    def worker():
        global _challenge_browser_active_user
        found_url = None
        found_detail = None
        try:
            os.makedirs(PLAYWRIGHT_PROFILE_DIR, exist_ok=True)
            with sync_playwright() as pw:
                context = pw.chromium.launch_persistent_context(
                    PLAYWRIGHT_PROFILE_DIR,
                    headless=False,
                    user_agent=HTTP_POLLER_USER_AGENT,
                    viewport={"width": 1100, "height": 800},
                )
                page = context.new_page()
                page.goto(target, timeout=30000)

                deadline = time.time() + CHALLENGE_SOLVE_TIMEOUT
                last_nudge = 0
                cleared = False
                while time.time() < deadline:
                    html = page.content()
                    tag_hits = [eid for eid in HTTP_POLLER_HYDRATION_IDS if eid in html]
                    if tag_hits:
                        cleared = True
                        break
                    if time.time() - last_nudge > 15:
                        last_nudge = time.time()
                        root.after(0, lambda: log(
                            f"[Challenge] Still waiting on @{username} - complete any verification "
                            f"shown in the browser window.", "normal"
                        ))
                    time.sleep(1.5)

                if not cleared:
                    found_detail = "timed out waiting for manual verification"
                else:
                    html = page.content()
                    for element_id in HTTP_POLLER_HYDRATION_IDS:
                        data = _extract_hydration_json(html, element_id)
                        if data is None:
                            continue
                        url, detail = _resolve_live_url_from_hydration(data, element_id, preferred_rank, allow_below)
                        if url is not None:
                            found_url, found_detail = url, detail
                            break
                        if detail is not None:
                            # Concrete negative (ended/inactive/no matching
                            # tier) - trust it, same as fetch_stream_url_http.
                            found_detail = detail
                            break
                        # detail is None: no FLV URLs in this blob at all,
                        # try the next hydration id.
                    if found_url is None and found_detail is None:
                        found_detail = "verification cleared, but no live stream data was present"

                    save_cookies_from_playwright(context.cookies())
                    root.after(0, lambda: log(f"[Challenge] Session cookies refreshed from browser for @{username}.", "good"))

                context.close()
        except Exception as e:
            found_detail = f"browser automation error: {e}"
        finally:
            with _challenge_browser_lock:
                _challenge_browser_active_user = None

        def finish():
            if found_url:
                log(f"[Challenge] @{username} is LIVE! Starting recording...", "good")
                start_recording(url=found_url, username=username)
            else:
                log(f"[Challenge] @{username}: {found_detail}", "warn")

        root.after(0, finish)

    threading.Thread(target=worker, daemon=True).start()


def prompt_manual_challenge_browser(username, preferred_rank, allow_below):
    # Only reached now when the auto-poller's bot-check is recurring faster
    # than AUTO_CHALLENGE_COOLDOWN allows a silent auto-open - i.e. the
    # unattended path already tried and it wasn't enough. Deduped per
    # username so a fast poll interval can't stack multiple dialogs.
    if is_shutting_down:
        return
    key = username.lower()
    if key in _challenge_prompt_pending:
        return
    _challenge_prompt_pending.add(key)

    dlg = tk.Toplevel(root)
    dlg.title("Verification Required")
    dlg.configure(bg=SURFACE)
    dlg.transient(root)
    dlg.grab_set()
    dlg.resizable(False, False)

    def cleanup():
        _challenge_prompt_pending.discard(key)

    dlg.protocol("WM_DELETE_WINDOW", lambda: (cleanup(), dlg.destroy()))

    tk.Label(
        dlg, text=f"TikTok is showing a bot-check page for @{username}.",
        font=("Segoe UI", 11, "bold"), fg=FG, bg=SURFACE
    ).pack(padx=24, pady=(20, 4))
    tk.Label(
        dlg, text="Opening a browser window lets you solve it by hand.\n"
                   "Recording starts automatically once the page loads.",
        font=FONT_LABEL, fg=FG_DIM, bg=SURFACE, justify="left"
    ).pack(padx=24, pady=(0, 16))

    btn_frame = tk.Frame(dlg, bg=SURFACE)
    btn_frame.pack(padx=16, pady=(0, 18))

    def choose_open():
        cleanup()
        dlg.destroy()
        run_manual_challenge_browser(username, preferred_rank, allow_below)

    def choose_cancel():
        cleanup()
        dlg.destroy()

    tk.Button(
        btn_frame, text="Open Browser", font=FONT_BTN, bg=FG_GOOD, fg="#000000",
        activebackground="#2cb865", activeforeground="#000000",
        relief="flat", bd=0, padx=16, pady=8, cursor="hand2",
        command=choose_open
    ).pack(side="left", padx=(0, 8))

    tk.Button(
        btn_frame, text="Cancel", font=FONT_LABEL, bg=SURFACE, fg=FG_DIM,
        activebackground=SURFACE, activeforeground=FG,
        relief="flat", bd=0, padx=12, pady=8, cursor="hand2",
        command=choose_cancel
    ).pack(side="left")


def check_session_health_on_startup():
    """Fires one lightweight HTTP probe shortly after launch to find out
    whether the saved session still clears TikTok's bot-check, so the
    manual-solve browser can be offered right away instead of waiting for
    the first real Check Live / auto-poll to fail."""
    def worker():
        if not os.path.exists(COOKIES_FILE):
            root.after(0, lambda: log(
                "[Startup] No saved session (cookies.json not found) - "
                "the first live check will likely need a manual browser solve.",
                "normal"
            ))
            return

        # Probe against whichever target is already configured, since that's
        # what real polling will hit anyway. Falls back to any handle typed
        # into the input box, then skips entirely if there's nothing to test.
        probe_username = None
        if monitored_targets_data:
            first_key = next(iter(monitored_targets_data))
            probe_username = monitored_targets_data[first_key].get("username", first_key)
        else:
            typed = get_handle_entry_value()
            if typed:
                probe_username = normalize_username(typed)
        if not probe_username:
            root.after(0, lambda: log(
                "[Startup] Skipping session check - no target configured to test against.",
                "normal"
            ))
            return

        root.after(0, lambda: log(f"[Startup] Testing saved session against @{probe_username}...", "normal"))
        pref_rank, allow_lower = get_effective_quality_settings(probe_username)
        with http_request_lock:
            url, detail = fetch_stream_url_http(probe_username, pref_rank, allow_lower)

        if detail == CHALLENGE_PAGE_DETAIL:
            root.after(0, lambda: log(
                "[Startup] Bot-check is active for this session - opening a browser to solve it automatically.",
                "warn"
            ))
            root.after(0, lambda: run_manual_challenge_browser(probe_username, pref_rank, allow_lower))
        elif url:
            # Session is fine and the probe target happens to be live right
            # now - report it, but don't start_recording() here. This check
            # exists to test the session, not to launch recordings; a target
            # that's actually live gets picked up by its own auto-monitor
            # poll or a manual Check Live, same as always.
            root.after(0, lambda: log(
                f"[Startup] Session is valid - bot-check cleared. (@{probe_username} is also currently live; "
                f"use Check Live or its own automation to start recording it.)",
                "good"
            ))
        else:
            root.after(0, lambda: log(
                f"[Startup] Session looks valid - bot-check not currently required ({detail}).",
                "good"
            ))

    threading.Thread(target=worker, daemon=True).start()


def set_target_favorite(username):
    key = username.lower()
    cfg = monitored_targets_data.get(key, {})
    cfg.update({
        "username": username,
        "mode": "favorite",
        "interval": cfg.get("interval", poll_interval_seconds),
        "schedule_enabled": cfg.get("schedule_enabled", False),
        "schedules": cfg.get("schedules", [{"start_time": "05:00 PM", "end_time": "11:30 PM"}]),
    })
    for tid, t in list(active_tasks.items()):
        if t.get("username") and t["username"].lower() == key and not t.get("proc"):
            stop_task(tid, reason="Stopped: switched to Favorite mode", prompt_resume=False)
    start_auto_monitor(username, cfg)
    refresh_inactive_target_tag(username)
    log(f"[Fetch] @{username} saved as Favorite (manual Check Live only, no auto-polling).", "good")


def prompt_offline_action(username):
    if is_shutting_down:
        return

    dlg = tk.Toplevel(root)
    dlg.title("User Offline")
    dlg.configure(bg=SURFACE)
    dlg.transient(root)
    dlg.grab_set()
    dlg.resizable(False, False)

    tk.Label(
        dlg, text=f"@{username} is currently offline.",
        font=("Segoe UI", 11, "bold"), fg=FG, bg=SURFACE
    ).pack(padx=24, pady=(20, 4))
    tk.Label(
        dlg, text="How should this target be tracked?",
        font=FONT_LABEL, fg=FG_DIM, bg=SURFACE
    ).pack(padx=24, pady=(0, 4))
    tk.Label(
        dlg, text="Automate: polls in the background and auto-records when live.\n"
                   "Favorite: sits in Inactive Targets, only records when you press Check Live.",
        font=FONT_LABEL, fg=FG_DIM, bg=SURFACE, justify="left"
    ).pack(padx=24, pady=(0, 16))

    btn_frame = tk.Frame(dlg, bg=SURFACE)
    btn_frame.pack(padx=16, pady=(0, 18))

    def choose_automate():
        dlg.destroy()
        start_auto_monitor(username, open_settings_if_new=True)
        log(f"[Fetch] @{username} set to Automate (polling every {poll_interval_seconds}s).", "good")

    def choose_favorite():
        dlg.destroy()
        set_target_favorite(username)

    tk.Button(
        btn_frame, text="Automate", font=FONT_BTN, bg=FG_GOOD, fg="#000000",
        activebackground="#2cb865", activeforeground="#000000",
        relief="flat", bd=0, padx=16, pady=8, cursor="hand2",
        command=choose_automate
    ).pack(side="left", padx=(0, 8))

    tk.Button(
        btn_frame, text="\u2605 Favorite", font=FONT_BTN, bg="#333333", fg="#ffcc00",
        activebackground="#444444", activeforeground="#ffcc00",
        relief="flat", bd=0, padx=16, pady=8, cursor="hand2",
        command=choose_favorite
    ).pack(side="left", padx=(0, 8))

    tk.Button(
        btn_frame, text="Cancel", font=FONT_LABEL, bg=SURFACE, fg=FG_DIM,
        activebackground=SURFACE, activeforeground=FG,
        relief="flat", bd=0, padx=12, pady=8, cursor="hand2",
        command=dlg.destroy
    ).pack(side="left")


def fetch_live_and_record():
    if is_shutting_down:
        log("[Shutdown] Action cancelled (shutdown in progress).", "warn")
        return

    raw = get_handle_entry_value()
    if not raw:
        log("Enter a handle first, then press Fetch Live.", "warn")
        return
    username = normalize_username(raw)
    if not username:
        log("Couldn't parse a username out of that input.", "warn")
        return

    preferred_key = QUALITY_LABEL_TO_KEY.get(quality_var.get(), "origin")
    allow_lower = allow_lower_var.get()
    GLOBAL_QUALITY_SETTINGS["preferred"] = preferred_key
    GLOBAL_QUALITY_SETTINGS["allow_lower"] = allow_lower
    save_quality_settings()
    preferred_rank = QUALITY_RANK.get(preferred_key, QUALITY_RANK["origin"])

    fetch_btn.config(state="disabled", text="Fetching...")
    log(f"[Fetch] Looking up @{username}'s live room over HTTP ({quality_var.get()})...", "normal")

    def worker():
        url, detail = fetch_stream_url_http(username, preferred_rank, allow_lower)

        def finish():
            fetch_btn.config(state="normal", text="Fetch Live")
            if is_shutting_down:
                return
            if url:
                log(f"[Fetch] {get_quality_label(url)} stream found for @{username} (via {detail}). Starting recording...", "good")
                persist_transcribe_preference(username, transcribe_active)
                start_recording(url=url, username=username, force_transcribe=transcribe_active)
                clear_handle_entry()
            elif detail == CHALLENGE_PAGE_DETAIL:
                log(f"[Fetch] @{username}: {detail} - opening a browser to solve it automatically.", "warn")
                run_manual_challenge_browser(username, preferred_rank, allow_lower)
            else:
                log(f"[Fetch] @{username} is offline or has nothing matching the quality setting ({detail}).", "normal")
                prompt_offline_action(username)

        root.after(0, finish)

    threading.Thread(target=worker, daemon=True).start()


# ── UI ────────────────────────────────────────────────────────────────────────

APP_WIDTH = 700

root = tk.Tk()
root.title("TikTok Live Recorder (Multi-Stream)")
root.geometry(f"{APP_WIDTH}x680")
root.resizable(True, True)
root.configure(bg="#1a1a1a")

FONT_TITLE  = ("Segoe UI", 13, "bold")
FONT_LABEL  = ("Segoe UI", 9)
FONT_MONO   = ("Consolas", 9)
FONT_BTN    = ("Segoe UI", 10, "bold")
FONT_STATUS = ("Segoe UI", 9)
FONT_HANDLE = ("Segoe UI", 10, "bold")

BG      = "#1a1a1a"
SURFACE = "#242424"
BORDER  = "#333333"
ACCENT  = "#ff2d55"
FG      = "#ffffff"
FG_DIM  = "#888888"
FG_GOOD = "#3ddc84"
FG_WARN = "#ffcc00"

root.protocol("WM_DELETE_WINDOW", initiate_shutdown)

# Header
header = tk.Frame(root, bg=BG)
header.pack(fill="x", padx=20, pady=(18, 0))
tk.Label(header, text="TikTok Live Recorder", font=FONT_TITLE, fg=FG, bg=BG).pack(side="left")
tk.Label(header, text="  configurable quality · multi-stream", font=FONT_LABEL, fg=FG_DIM, bg=BG).pack(side="left")

# Top Right Red Shutdown Rectangle Button
btn_shutdown = tk.Button(
    header, text="⛔ SHUTDOWN", font=("Segoe UI", 10, "bold"),
    bg="#cc0033", fg="#ffffff", activebackground="#990022", activeforeground="#ffffff",
    relief="flat", bd=0, padx=14, pady=4, cursor="hand2",
    command=initiate_shutdown
)
btn_shutdown.pack(side="right")

# Handle input
url_frame = tk.Frame(root, bg=SURFACE, bd=0, highlightthickness=1, highlightbackground=BORDER)
url_frame.pack(fill="x", padx=20, pady=(14, 0))

url_header = tk.Frame(url_frame, bg=SURFACE)
url_header.pack(fill="x", padx=10, pady=(8, 2))

tk.Label(url_header, text="Handle", font=FONT_LABEL, fg=FG_DIM, bg=SURFACE).pack(side="left")

inputs_row = tk.Frame(url_frame, bg=SURFACE)
inputs_row.pack(fill="x", padx=10, pady=(0, 4))

handle_var = tk.StringVar(value=HANDLE_PLACEHOLDER)

handle_entry = tk.Entry(
    inputs_row, font=FONT_HANDLE, bg="#2a2a2a", fg=FG_DIM,
    insertbackground=FG, relief="flat", bd=0, highlightthickness=0,
    width=26, textvariable=handle_var
)
handle_entry.pack(side="left", ipady=6, padx=(0, 8))

handle_entry.bind("<FocusIn>", on_handle_focus_in)
handle_entry.bind("<FocusOut>", on_handle_focus_out)
handle_entry.bind("<KeyRelease>", lambda e: root.after_idle(format_handle_input))
handle_entry.bind("<<Paste>>", on_handle_paste)
handle_entry.bind("<Return>", lambda _e: fetch_live_and_record())

tk.Label(inputs_row, text="QUALITY", font=FONT_LABEL, fg=FG_DIM, bg=SURFACE).pack(side="left", padx=(0, 4))

quality_var = tk.StringVar(value=QUALITY_LABEL.get(GLOBAL_QUALITY_SETTINGS["preferred"], "Best (Origin)"))
quality_combo = ttk.Combobox(
    inputs_row, textvariable=quality_var, values=QUALITY_LABELS_ORDERED,
    width=13, state="readonly"
)
quality_combo.pack(side="left", padx=(0, 8))


def _on_quality_change(_e=None):
    GLOBAL_QUALITY_SETTINGS["preferred"] = QUALITY_LABEL_TO_KEY.get(quality_var.get(), "origin")
    save_quality_settings()


quality_combo.bind("<<ComboboxSelected>>", _on_quality_change)

fetch_btn = tk.Button(
    inputs_row, text="Fetch Live", font=FONT_LABEL,
    bg=SURFACE, fg=FG_GOOD, activebackground="#333333", activeforeground=FG,
    relief="flat", bd=0, padx=10, pady=6, cursor="hand2",
    command=fetch_live_and_record
)
fetch_btn.pack(side="left")

toggle_row = tk.Frame(url_frame, bg=SURFACE)
toggle_row.pack(fill="x", padx=10, pady=(0, 8))

allow_lower_var = tk.BooleanVar(value=GLOBAL_QUALITY_SETTINGS["allow_lower"])


def _on_allow_lower_toggle():
    GLOBAL_QUALITY_SETTINGS["allow_lower"] = allow_lower_var.get()
    save_quality_settings()


cb_allow_lower = tk.Checkbutton(
    toggle_row, text="Fall back to lower quality if needed", variable=allow_lower_var,
    font=FONT_LABEL, fg=FG_DIM, bg=SURFACE, activebackground=SURFACE,
    activeforeground=FG, selectcolor="#2a2a2a", bd=0, highlightthickness=0,
    cursor="hand2", command=_on_allow_lower_toggle
)
cb_allow_lower.pack(side="left")

# Buttons Panel
btn_frame = tk.Frame(root, bg=BG)
btn_frame.pack(fill="x", padx=20, pady=12)


def cycle_poll_interval():
    global poll_interval_index, poll_interval_seconds
    poll_interval_index = (poll_interval_index + 1) % len(POLL_INTERVAL_PRESETS)
    poll_interval_seconds = POLL_INTERVAL_PRESETS[poll_interval_index]
    display_str = f"{poll_interval_seconds}s" if poll_interval_seconds < 60 else f"{poll_interval_seconds//60}m"
    poll_btn.config(text=f"Auto Poll: {display_str}")
    log(f"[Auto] Default polling interval updated to {display_str}.", "normal")


poll_btn = tk.Button(
    btn_frame, text="Auto Poll: 30s", font=FONT_BTN,
    bg=SURFACE, fg=FG_GOOD, activebackground="#333333", activeforeground=FG,
    relief="flat", bd=0, padx=16, pady=8, cursor="hand2",
    command=cycle_poll_interval
)
poll_btn.pack(side="left", padx=(0, 8))


def toggle_transcribe():
    global transcribe_active
    transcribe_active = not transcribe_active
    if transcribe_active:
        transcribe_btn.config(text="Transcribe: ON", fg=FG_GOOD)
    else:
        transcribe_btn.config(text="Transcribe: OFF", fg="#ff5555")


transcribe_btn = tk.Button(
    btn_frame,
    text=("Transcribe: ON" if transcribe_active else "Transcribe: OFF"),
    font=FONT_BTN,
    bg=SURFACE, fg=(FG_GOOD if transcribe_active else "#ff5555"),
    activebackground="#333333", activeforeground=FG,
    relief="flat", bd=0, padx=16, pady=8, cursor="hand2",
    command=toggle_transcribe
)
transcribe_btn.pack(side="left", padx=(0, 8))


tk.Button(
    btn_frame, text="Open Folder", font=FONT_BTN,
    bg=SURFACE, fg=FG_DIM, activebackground="#333333", activeforeground=FG,
    relief="flat", bd=0, padx=16, pady=8, cursor="hand2",
    command=open_recordings
).pack(side="left", padx=(0, 8))

tk.Button(
    btn_frame, text="Clear Log", font=FONT_LABEL,
    bg=BG, fg=FG_DIM, activebackground=BG, activeforeground=FG,
    relief="flat", bd=0, padx=10, pady=8, cursor="hand2",
    command=clear_log
).pack(side="right")

# Scrollbar trough blends into the app's base background; only the grey thumb
# itself should read as visible, and it should NOT match the lighter grey
# used for the active/inactive row surfaces.
SCROLLBAR_THUMB = "#3a3a3a"

def fit_window_to_content():
    """Resizes the window's height to exactly what its content needs right
    now, keeping the current width and screen position untouched."""
    try:
        root.update_idletasks()
        geom = root.geometry()
        m = re.match(r"^(\d+)x(\d+)(.*)$", geom)
        w = int(m.group(1)) if m else APP_WIDTH
        rest = m.group(3) if m else ""
        new_h = root.winfo_reqheight()
        root.geometry(f"{w}x{new_h}{rest}")
    except Exception:
        pass


def make_scrollable_list(parent, max_visible_items, row_pady, sample_font, label_pady):
    """Builds a Canvas+inner-Frame scroll region that grows and shrinks with
    its real row count - up to max_visible_items rows tall (measured once
    from a throwaway sample row built with the same font/padding real rows
    use, so it's pixel-accurate regardless of font/DPI). Past that cap it
    stops growing and a scrollbar appears. Every size change also re-fits
    the window so there's no leftover dead space, and no manual resizing
    needed as recordings start/stop."""
    outer = tk.Frame(parent, bg=SURFACE, bd=0, highlightthickness=1, highlightbackground=BORDER)

    canvas = tk.Canvas(outer, bg=SURFACE, highlightthickness=0, bd=0)
    scrollbar = tk.Scrollbar(
        outer, orient="vertical", command=canvas.yview,
        bg=SCROLLBAR_THUMB, activebackground=SCROLLBAR_THUMB,
        troughcolor=BG, highlightthickness=0, highlightbackground=BG,
        bd=0, relief="flat", width=10
    )
    inner = tk.Frame(canvas, bg=SURFACE)

    inner_window = canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)
    canvas.pack(side="left", fill="both", expand=True, padx=(5, 0), pady=3)

    # Measure one real-looking row, then throw it away - this fixes the cap
    # in pixels before any actual rows/empty-state label exist.
    sample = tk.Frame(inner, bg="#222222")
    sample.pack(fill="x", pady=row_pady, padx=5)
    tk.Label(sample, text="@sample", font=sample_font, bg="#222222", anchor="w").pack(side="left", padx=12, pady=label_pady)
    sample.update_idletasks()
    row_h = sample.winfo_reqheight() + (2 * row_pady)
    sample.destroy()
    cap_height = row_h * max_visible_items

    def _on_inner_configure(_e=None):
        canvas.configure(scrollregion=canvas.bbox("all"))
        content_h = inner.winfo_reqheight()
        canvas.configure(height=min(content_h, cap_height))
        if content_h > cap_height:
            scrollbar.pack(side="right", fill="y")
        else:
            scrollbar.pack_forget()
            canvas.yview_moveto(0)
        root.after_idle(fit_window_to_content)

    def _on_canvas_configure(event):
        canvas.itemconfig(inner_window, width=event.width)

    inner.bind("<Configure>", _on_inner_configure)
    canvas.bind("<Configure>", _on_canvas_configure)

    def _mousewheel(event):
        if inner.winfo_reqheight() > cap_height:
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _bind_wheel(_e):
        canvas.bind_all("<MouseWheel>", _mousewheel)

    def _unbind_wheel(_e):
        canvas.unbind_all("<MouseWheel>")

    canvas.bind("<Enter>", _bind_wheel)
    canvas.bind("<Leave>", _unbind_wheel)

    return outer, inner


# Active Recordings Display Frame
active_lbl = tk.Label(root, text="Active Recordings", font=FONT_TITLE, fg=FG, bg=BG, anchor="w")
active_lbl.pack(fill="x", padx=20, pady=(6, 2))

active_outer, active_list_frame = make_scrollable_list(
    root, max_visible_items=4, row_pady=4,
    sample_font=("Segoe UI", 10, "bold"), label_pady=8
)
active_outer.pack(fill="x", padx=20, pady=(0, 6))

empty_lbl = tk.Label(active_list_frame, text="No streams currently recording.", font=FONT_LABEL, fg=FG_DIM, bg=SURFACE)
empty_lbl.pack(fill="x", pady=6)

# Inactive Targets Display Frame
inactive_lbl = tk.Label(root, text="Inactive Targets & Saved Automations", font=FONT_TITLE, fg=FG_DIM, bg=BG, anchor="w")
inactive_lbl.pack(fill="x", padx=20, pady=(4, 2))

inactive_outer, inactive_list_frame = make_scrollable_list(
    root, max_visible_items=4, row_pady=3,
    sample_font=("Segoe UI", 10, "bold"), label_pady=6
)
inactive_outer.pack(fill="x", padx=20, pady=(0, 8))

inactive_empty_lbl = tk.Label(inactive_list_frame, text="No saved inactive targets.", font=FONT_LABEL, fg=FG_DIM, bg=SURFACE)
inactive_empty_lbl.pack(fill="x", pady=6)

# Collapsible Debug Log Panel Toggle Button at Bottom
bottom_bar = tk.Frame(root, bg=BG)
bottom_bar.pack(fill="x", padx=20, pady=(4, 4))

btn_debug = tk.Button(
    bottom_bar, text="Show Debug Log ▾", font=FONT_LABEL, bg=SURFACE, fg=FG_DIM,
    activebackground="#333333", activeforeground=FG, relief="flat", bd=0, padx=12, pady=4, cursor="hand2",
    command=toggle_debug_log
)
btn_debug.pack(side="left")

# Collapsible Log Frame (Hidden by default)
log_frame = tk.Frame(root, bg=SURFACE, bd=0, highlightthickness=1, highlightbackground=BORDER)

log_box = scrolledtext.ScrolledText(
    log_frame, font=FONT_MONO, bg="#1e1e1e", fg=FG,
    relief="flat", bd=0, state="disabled",
    wrap="word", padx=10, pady=10
)
log_box.pack(fill="both", expand=True)

log_box.tag_config("normal", foreground=FG)
log_box.tag_config("good",   foreground=FG_GOOD)
log_box.tag_config("warn",   foreground=FG_WARN)

log("TikTok Live Recorder ready (Multi-Stream & Auto-Monitor Mode).", "good")
log(f"Recordings saved to: {OUTPUT_DIR}", "normal")
log("------------------------------------------------------------", "normal")
log("AUTOMATION ENGINE ACTIVE:", "good")
log("• Add a handle to auto-monitor it, or use the Fetch Live button.", "good")
log("• Persistent monitoring checks offline users and auto-records when Live.", "good")
log("• Adjustable polling interval (15s to 5m) via the 'Auto Poll' button.", "normal")
log("• Links meeting the Quality setting start FFMPEG automatically.", "normal")
log("• Bot-check pages are solved automatically with a browser window when needed.", "normal")
log("------------------------------------------------------------", "normal")

# Load persistent quality defaults and targets
load_quality_settings()
quality_var.set(QUALITY_LABEL.get(GLOBAL_QUALITY_SETTINGS["preferred"], "Best (Origin)"))
allow_lower_var.set(GLOBAL_QUALITY_SETTINGS["allow_lower"])
load_monitored_targets()
check_active_scheduled_targets_on_startup()

for key_name, target_info in list(monitored_targets_data.items()):
    u_name = target_info.get("username", key_name)
    start_auto_monitor(u_name, target_info)

# Recover leftover raw .flv files from previous session
threading.Thread(target=recover_orphaned_flvs, daemon=True).start()

# Probe the saved session once at startup so a stale/challenged session is
# caught immediately instead of surfacing as a confusing "offline" on the
# first real check.
root.after(2000, check_session_health_on_startup)

# Snap the window to the height its content actually needs at startup - the
# active/inactive lists keep it correct automatically from here on as rows
# come and go (see fit_window_to_content, called from make_scrollable_list).
fit_window_to_content()

root.mainloop()