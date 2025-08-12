# youtube_gui_downloader.py
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import threading
import subprocess
import shlex
import os
import re
import sys
import signal
import time



# ---------- Globals to control worker ----------
worker_thread = None
worker_proc = None
worker_lock = threading.Lock()
is_paused = False
is_stopped = False

# ---------- Helper functions ----------
def choose_folder():
    folder = filedialog.askdirectory()
    if folder:
        path_var.set(folder)

def build_format_choice(choice):
    """
    Choose format strings that prefer single-file containers to avoid ffmpeg merging.
    Fallback uses 'best' if perfect single-file not available.
    """
    if choice == "Best":
        return "best"
    if choice == "1080p":
        # prefer mp4 single-file at <=1080, fallback to best
        return "best[ext=mp4][height<=1080]/best[height<=1080]/best"
    if choice == "720p":
        return "best[ext=mp4][height<=720]/best[height<=720]/best"
    if choice == "Audio only":
        # prefer m4a single-file audio
        return "bestaudio[ext=m4a]/bestaudio"
    return "best"

def parse_progress_line(line):
    """
    Parse yt-dlp --newline progress lines, example patterns:
      [download]   3.4% of 4.08MiB at  1.23MiB/s ETA 00:03
      [download] 100% of 4.08MiB in 00:03
    We extract percent (float), speed, eta, and status text.
    """
    # common percent pattern
    m = re.search(r'(\d{1,3}\.\d|\d{1,3})\%', line)
    percent = None
    speed = None
    eta = None
    if m:
        try:
            percent = float(m.group(1))
        except:
            percent = None

    # speed like '1.23MiB/s'
    m2 = re.search(r'at\s+([0-9\.]+\w+/s)', line)
    if m2:
        speed = m2.group(1)

    # ETA like 'ETA 00:03' or 'in 00:03'
    m3 = re.search(r'ETA\s*([0-9:\.]+)', line)
    if not m3:
        m3 = re.search(r'in\s+([0-9:\.]+)', line)
    if m3:
        eta = m3.group(1)

    return percent, speed, eta, line.strip()

def update_status(text):
    status_label.config(text=text)

def safe_kill_proc(proc):
    """Terminate process cross-platform safely"""
    try:
        if proc.poll() is None:
            # try terminate gracefully
            proc.terminate()
            # give little time
            time.sleep(0.3)
            if proc.poll() is None:
                proc.kill()
    except Exception:
        pass

# ---------- Worker: runs yt-dlp as subprocess and updates GUI ----------
def download_worker(url, out_path, quality_choice):
    global worker_proc, is_paused, is_stopped

    # prepare output template
    outtmpl = os.path.join(out_path, '%(title)s.%(ext)s')

    # format
    fmt = build_format_choice(quality_choice)

    # base command parts
    cmd = [
        "yt-dlp",
        "--newline",            # important: ensure progress comes as separate lines
        "--no-warnings",
        "--no-overwrites",
        "-o", outtmpl,
        "-f", fmt,
        url
    ]

    # if Audio only: also write thumbnail (don't embed to avoid ffmpeg requirements)
    if quality_choice == "Audio only":
        # write thumbnail as a separate file (no embedding)
        cmd.insert(-1, "--write-thumbnail")
        # choose audio container m4a or let yt-dlp handle it via format preference
        # avoid postprocessors that require ffmpeg (so we don't extract to mp3 here)
        # user will get the audio in m4a if available

    # Convert to string for subprocess with shell=False (safer)
    # Note: on Windows, ensure yt-dlp is in PATH; otherwise call full path to yt-dlp.exe
    update_status("Preparing download...")

    # Start subprocess
    with worker_lock:
        is_paused = False
        is_stopped = False
        try:
            worker_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                universal_newlines=True
            )
        except FileNotFoundError as e:
            update_status("yt-dlp not found. Install yt-dlp and ensure it's in PATH.")
            messagebox.showerror("Error", "yt-dlp executable not found. Run `pip install yt-dlp` and ensure it's in PATH.")
            worker_proc = None
            return
        except Exception as e:
            update_status(f"Failed to start yt-dlp: {e}")
            messagebox.showerror("Error", f"Failed to start yt-dlp: {e}")
            worker_proc = None
            return

    # read stdout line-by-line
    try:
        for raw_line in worker_proc.stdout:
            line = raw_line.rstrip('\n')
            # handle stop/pause checks
            if is_stopped:
                update_status("Stopping download...")
                safe_kill_proc(worker_proc)
                break
            if is_paused:
                # pause behavior: kill process but keep partial files for resume
                update_status("Paused. Partial file kept. Click Resume to continue.")
                safe_kill_proc(worker_proc)
                break

            # parse progress and update GUI
            percent, speed, eta, text = parse_progress_line(line)
            if percent is not None:
                # update progressbar
                try:
                    progress_var.set(percent)
                    prog_text = f"Downloading: {percent:.1f}%"
                    if speed:
                        prog_text += f" | {speed}"
                    if eta:
                        prog_text += f" | ETA {eta}"
                    update_status(prog_text)
                except Exception:
                    pass
            else:
                # some other informative line - show briefly
                update_status(text)

            # ensure UI updates
            root.update_idletasks()

        # wait for process end
        if worker_proc:
            worker_proc.wait()
            rc = worker_proc.returncode
        else:
            rc = None

        # finalization logic
        if is_stopped:
            update_status("Stopped by user.")
            progress_var.set(0)
        elif is_paused:
            # paused; don't show finished
            pass
        else:
            if rc == 0:
                progress_var.set(100)
                update_status("Download finished.")
                messagebox.showinfo("Done", "Download completed successfully.")
            else:
                # non-zero rc: error likely printed to stdout already
                update_status(f"yt-dlp finished with code {rc}. Check terminal for details.")
                messagebox.showwarning("Finished", f"yt-dlp exited with code {rc}. See terminal for details.")

    except Exception as e:
        update_status(f"Error during download: {e}")
        safe_kill_proc(worker_proc)
        messagebox.showerror("Error", f"Error during download: {e}")
    finally:
        with worker_lock:
            worker_proc = None

# ---------- Thread control functions ----------
def start_download():
    global worker_thread, is_paused, is_stopped
    url = url_var.get().strip()
    out_path = path_var.get().strip() or os.getcwd()
    quality_choice = quality_var.get()

    if not url:
        messagebox.showerror("Error", "Please enter a YouTube URL.")
        return
    if not os.path.isdir(out_path):
        messagebox.showerror("Error", "Please choose a valid folder to save downloads.")
        return

    # if a worker is already running, warn
    with worker_lock:
        if worker_proc is not None:
            messagebox.showwarning("Busy", "A download is already running. Stop it first to start a new one.")
            return

    progress_var.set(0)
    update_status("Queued. Starting...")
    is_paused = False
    is_stopped = False
    # spawn thread
    worker_thread = threading.Thread(target=download_worker, args=(url, out_path, quality_choice), daemon=True)
    worker_thread.start()

def pause_download():
    global is_paused
    with worker_lock:
        if worker_proc is None:
            update_status("No active download to pause.")
            return
        # set flag; worker will kill process and keep partial file
        is_paused = True
    update_status("Pausing...")

def resume_download():
    global is_paused, worker_thread
    with worker_lock:
        if worker_proc is not None:
            update_status("Download already running.")
            return
        if not is_paused:
            update_status("Nothing to resume.")
            return
        # resume by starting a new worker thread with same inputs
        is_paused = False
    update_status("Resuming download...")
    # reuse current GUI inputs (URL/path/quality)
    worker_thread = threading.Thread(target=download_worker, args=(url_var.get().strip(), path_var.get().strip() or os.getcwd(), quality_var.get()), daemon=True)
    worker_thread.start()

def stop_download():
    global is_stopped
    with worker_lock:
        if worker_proc is None:
            update_status("No active download to stop.")
            return
        is_stopped = True
    update_status("Stopping download...")

# ---------- GUI layout ----------
# ---------- GUI layout ----------
root = tk.Tk()
root.title("YouTube Downloader")
root.geometry("700x320")
root.resizable(False, False)

icon_path = os.path.join(os.path.dirname(__file__), "icon.ico")
if os.path.exists(icon_path):
    root.iconbitmap(icon_path)
else:
    print("Warning: icon.ico not found, skipping custom icon.")

frm = tk.Frame(root, padx=12, pady=8)
frm.pack(fill=tk.BOTH, expand=True)

# Make 4 columns expand equally so widgets center
for i in range(4):
    frm.grid_columnconfigure(i, weight=1)

tk.Label(frm, text="YouTube URL:").grid(row=0, column=0)
url_var = tk.StringVar()
tk.Entry(frm, textvariable=url_var, width=70, justify="center").grid(row=0, column=1, columnspan=3, padx=6, pady=4)

tk.Label(frm, text="Save to:").grid(row=1, column=0)
path_var = tk.StringVar(value=os.getcwd())
tk.Entry(frm, textvariable=path_var, width=52, justify="center").grid(row=1, column=1, padx=6)
tk.Button(frm, text="Browse", command=choose_folder, width=10).grid(row=1, column=2)

tk.Label(frm, text="Quality:").grid(row=2, column=0, pady=(8,0))
quality_var = tk.StringVar(value="Best")
quality_combo = ttk.Combobox(frm, textvariable=quality_var, values=["Best", "1080p", "720p", "Audio only"], state="readonly", width=20)
quality_combo.grid(row=2, column=1, pady=(8,0))

# Progress bar & status
progress_var = tk.DoubleVar(value=0.0)
progress = ttk.Progressbar(frm, variable=progress_var, maximum=100, length=560)
progress.grid(row=3, column=0, columnspan=4, pady=(14,6))

status_label = tk.Label(frm, text="Status: Idle", anchor="center", justify="center", wraplength=660)
status_label.grid(row=4, column=0, columnspan=4)

# Buttons
btn_frame = tk.Frame(frm)
btn_frame.grid(row=5, column=0, columnspan=4, pady=12)

start_btn = tk.Button(btn_frame, text="Download", command=start_download, bg="#2ecc71", fg="white", width=12)
start_btn.pack(side="left", padx=6)
pause_btn = tk.Button(btn_frame, text="Pause", command=pause_download, bg="#f39c12", fg="white", width=12)
pause_btn.pack(side="left", padx=6)
resume_btn = tk.Button(btn_frame, text="Resume", command=resume_download, bg="#3498db", fg="white", width=12)
resume_btn.pack(side="left", padx=6)
stop_btn = tk.Button(btn_frame, text="Stop", command=stop_download, bg="#e74c3c", fg="white", width=12)
stop_btn.pack(side="left", padx=6)

# Footer note
tk.Label(frm, text="Note: Pause kills the running downloader but keeps partial files. Resume will continue.", fg="gray", anchor="center", justify="center").grid(row=6, column=0, columnspan=4)

# Ensure safe termination on close
def on_closing():
    global worker_proc, is_stopped
    if messagebox.askokcancel("Quit", "Do you want to quit? Any active download will be stopped."):
        with worker_lock:
            if worker_proc:
                is_stopped = True
                safe_kill_proc(worker_proc)
        root.destroy()

root.protocol("WM_DELETE_WINDOW", on_closing)

# Run the GUI
root.mainloop()
