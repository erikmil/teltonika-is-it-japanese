# Japanese Audit Scanner

A tool that scans the Teltonika GPS Japanese website (`teltonika-gps.com/ja/`) and finds:
- **English text** that was never translated into Japanese
- **Broken links** that are missing the `/ja/` part and would send visitors to the wrong language

You run it once, it crawls the whole site, then gives you a report you can share with the team.

---

## What you need before starting

- A **Mac** (these instructions are for macOS)
- **Python 3.12** installed on your Mac — see below if you're not sure
- An internet connection

### Check if Python 3.12 is installed

1. Open the **Terminal** app. You can find it by pressing `⌘ Space`, typing `Terminal`, and pressing Enter.

   > **What is Terminal?** It's a text-based way to give your Mac instructions. It looks like a black or white window with a blinking cursor. You type commands and press Enter to run them.

2. In Terminal, type the following and press Enter:
   ```
   python3.12 --version
   ```
3. If you see something like `Python 3.12.x` — you're good to go.
4. If you see an error, [download Python 3.12 here](https://www.python.org/downloads/) and run the installer, then restart Terminal and try again.

---

## Installation (one time only)

You only need to do these steps once. After that, skip straight to **Running the scanner**.

### Step 1 — Download the project

If you received a ZIP file, unzip it and put the folder somewhere easy to find, like your **Desktop** or **Documents** folder.

If you're using GitHub, click the green **Code** button → **Download ZIP**, then unzip it.

### Step 2 — Open Terminal and navigate to the project folder

1. Open **Terminal** (press `⌘ Space`, type `Terminal`, press Enter).
2. Type `cd ` (with a space after it — don't press Enter yet).
3. Open **Finder**, find the project folder (e.g. `teltonika-is-it-japanese`), and **drag the folder** into the Terminal window. The folder path will appear automatically.
4. Press **Enter**.

   > You've just told Terminal "go into this folder". This is the only navigation command you need. If you ever close Terminal and want to run the scanner again, repeat steps 1–4.

### Step 3 — Set up the environment (run once)

Copy and paste each line below into Terminal one at a time, pressing **Enter** after each one. Wait for each to finish before pasting the next.

```
/opt/homebrew/bin/python3.12 -m venv .venv
```
> This creates a private Python workspace for the scanner. You'll see a new hidden folder appear.

```
.venv/bin/pip install -r requirements.txt
```
> This downloads the tools the scanner needs. It may take 1–2 minutes. You'll see a lot of text scrolling — that's normal.

```
.venv/bin/playwright install chromium
```
> This downloads a headless browser the scanner uses to load JavaScript-heavy pages. May take another 1–2 minutes.

You're done with setup!

---

## Running the scanner

Every time you want to scan, open Terminal, navigate to the project folder (Step 2 above), then run:

```
.venv/bin/python3.12 app.py
```

You'll see a message like:
```
Uvicorn running on http://127.0.0.1:8001
```

Now open your web browser (Chrome, Safari, Firefox — any) and go to:

**http://localhost:8001**

---

## Using the scanner

1. Click **Start Scan** — the scanner will begin crawling the website. Pages appear live as they are processed.
2. Wait for the crawl to finish. A large site may take 15–30 minutes.
3. Click **Generate Report** when done.
4. A report file will be saved in the `data/reports/` folder inside the project. You can open it in any browser.

### Tips

- **Product codes** (like "FMB920", "GPS", "LTE") are normal English in a Japanese page — use the filter toggle to hide them from the report.
- You can stop and restart the scan at any time — it remembers where it left off.
- To start a completely fresh scan, click **Clear & Restart** before starting.

---

## Stopping the scanner

When you're done, go back to Terminal and press **Ctrl + C** (hold Control, press C). The server will stop.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `command not found: python3.12` | Install Python 3.12 from [python.org](https://www.python.org/downloads/) |
| Browser shows "This site can't be reached" | Make sure the scanner is still running in Terminal |
| Scan stops mid-way | Restart the scanner — it will resume from where it left off |
| `No such file or directory` when running `.venv/bin/python3.12` | You're not in the right folder — repeat Step 2 |

---

## Need help?

Contact the developer or open an issue on GitHub.
