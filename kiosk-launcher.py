#!/usr/bin/env python3
import os
import subprocess
import tkinter as tk
from tkinter import font

URL_FILE = "/home/meadow/kiosk.url"
DEFAULT_URL = "about:blank"
STOP_FLAG = "/tmp/meadow_kiosk_stop"
KIOSK_SCRIPT = "/home/meadow/kiosk-browser.sh"

def read_url() -> str:
    try:
        with open(URL_FILE, "r", encoding="utf-8") as f:
            url = f.readline().strip()
            return url or DEFAULT_URL
    except Exception:
        return DEFAULT_URL

def start_kiosk(root: tk.Tk) -> None:
    # Clear any previous stop request
    try:
        os.remove(STOP_FLAG)
    except FileNotFoundError:
        pass

    # Launch kiosk loop in the background (so the launcher can close)
    subprocess.Popen(["bash", KIOSK_SCRIPT], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    root.destroy()

def go_desktop(root: tk.Tk) -> None:
    root.destroy()

def main() -> None:
    url = read_url()

    root = tk.Tk()
    root.title("Meadow Kiosk Launcher")
    root.attributes("-fullscreen", True)
    root.configure(bg="#111111")
    root.bind("<Escape>", lambda e: root.destroy())

    # Typography
    title_font = font.Font(family="DejaVu Sans", size=34, weight="bold")
    url_font = font.Font(family="DejaVu Sans", size=18)
    btn_font = font.Font(family="DejaVu Sans", size=22, weight="bold")

    container = tk.Frame(root, bg="#111111")
    container.pack(expand=True, fill="both", padx=60, pady=60)

    title = tk.Label(container, text="Meadow Vending", fg="#ffffff", bg="#111111", font=title_font)
    title.pack(pady=(0, 18))

    url_label = tk.Label(
        container,
        text=f"Kiosk URL:\n{url}",
        fg="#bbbbbb",
        bg="#111111",
        font=url_font,
        justify="center",
        wraplength=1100,
    )
    url_label.pack(pady=(0, 40))

    btn_frame = tk.Frame(container, bg="#111111")
    btn_frame.pack()

    kiosk_btn = tk.Button(
        btn_frame,
        text="ENTER KIOSK MODE",
        font=btn_font,
        padx=40,
        pady=22,
        bd=0,
        bg="#2d7cff",
        fg="#ffffff",
        activebackground="#2d7cff",
        activeforeground="#ffffff",
        command=lambda: start_kiosk(root),
    )
    kiosk_btn.grid(row=0, column=0, padx=20, pady=10)

    desk_btn = tk.Button(
        btn_frame,
        text="GO TO DESKTOP",
        font=btn_font,
        padx=40,
        pady=22,
        bd=0,
        bg="#333333",
        fg="#ffffff",
        activebackground="#333333",
        activeforeground="#ffffff",
        command=lambda: go_desktop(root),
    )
    desk_btn.grid(row=0, column=1, padx=20, pady=10)

    hint = tk.Label(
        container,
        text="Tip: Ctrl+Alt+E exits kiosk mode anytime.",
        fg="#777777",
        bg="#111111",
        font=font.Font(family="DejaVu Sans", size=14),
    )
    hint.pack(pady=(40, 0))

    root.mainloop()

if __name__ == "__main__":
    main()
