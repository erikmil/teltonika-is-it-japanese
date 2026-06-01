# Japanese Audit Scanner

A tool that scans the Teltonika GPS Japanese website (`teltonika-gps.com/ja/`) and finds:
- **English text** that was never translated into Japanese
- **Broken links** that are missing the `/ja/` part and would send visitors to the wrong language

You run it once, it crawls the whole site, then gives you a report you can share with the team.

---

## Choose your operating system

- [I have a Mac](#mac-instructions)
- [I have Windows](#windows-instructions)

---

## Mac instructions

### What you need

- **Python 3.12** installed — see below if you're not sure
- An internet connection

### Step 0 — Check if Python 3.12 is installed

1. Open the **Terminal** app. Press `⌘ Space`, type `Terminal`, and press Enter.

   > **What is Terminal?** It's a text window where you type commands to control your Mac. It looks intimidating but you only need to copy and paste a few lines.

2. Type this and press Enter:
   ```
   python3.12 --version
   ```
3. If you see `Python 3.12.x` — you're good. Skip to Step 1.
4. If you see an error, [download Python 3.12 here](https://www.python.org/downloads/), run the installer, restart Terminal, and try again.

### Step 1 — Download the project

Click the green **Code** button on this GitHub page → **Download ZIP**, then unzip it. Move the folder somewhere easy to find, like your **Desktop** or **Documents**.

### Step 2 — Open Terminal in the project folder

1. Open **Terminal** (`⌘ Space` → type `Terminal` → Enter).
2. Type `cd ` (with a space after — don't press Enter yet).
3. Open **Finder**, find the project folder (`teltonika-is-it-japanese`), and **drag it into the Terminal window**. The folder path fills in automatically.
4. Press **Enter**.

   > You've just told Terminal "work inside this folder". If you close Terminal and come back later, repeat these 4 steps before running anything.

### Step 3 — Set up (run once)

Copy and paste each line into Terminal one at a time, pressing **Enter** after each. Wait for each to finish before the next.

```
/opt/homebrew/bin/python3.12 -m venv .venv
```
> Creates a private Python environment for the scanner.

```
.venv/bin/pip install -r requirements.txt
```
> Downloads the tools the scanner needs. Takes 1–2 minutes — lots of scrolling text is normal.

```
.venv/bin/playwright install chromium
```
> Downloads a headless browser used to load pages. Takes another 1–2 minutes.

You're done with setup!

### Step 4 — Run the scanner

Every time you want to scan, do Step 2 first (navigate to the folder), then run:

```
.venv/bin/python3.12 app.py
```

You'll see:
```
Uvicorn running on http://127.0.0.1:8001
```

Open any browser and go to **http://localhost:8001**

### Stopping the scanner (Mac)

Go back to Terminal and press **Ctrl + C** (hold Control, press C).

---

## Windows instructions

### What you need

- **Windows 10 or 11**
- **Python 3.12** installed — see below if you're not sure
- An internet connection

### Step 0 — Check if Python 3.12 is installed

1. Open **Command Prompt**. Press the `Windows` key, type `cmd`, and press Enter.

   > **What is Command Prompt?** It's a text window where you type commands to control your PC. You only need to copy and paste a few lines.

2. Type this and press Enter:
   ```
   py -3.12 --version
   ```
3. If you see `Python 3.12.x` — you're good. Skip to Step 1.
4. If you see an error, [download Python 3.12 here](https://www.python.org/downloads/). During installation, **check the box that says "Add Python to PATH"** before clicking Install. Then restart Command Prompt and try again.

### Step 1 — Download the project

Click the green **Code** button on this GitHub page → **Download ZIP**, then unzip it. Move the folder somewhere easy to find, like your **Desktop** or **Documents**.

### Step 2 — Open Command Prompt in the project folder

1. Open **File Explorer** and navigate to the project folder (`teltonika-is-it-japanese`).
2. Click the **address bar** at the top (the bar showing the folder path — it usually starts with `C:\...`).
3. Type `cmd` and press **Enter**.

   > Command Prompt opens automatically inside the project folder. You're ready.

   > If you close Command Prompt and come back later, repeat steps 1–3 before running anything.

### Step 3 — Set up (run once)

Copy and paste each line into Command Prompt one at a time, pressing **Enter** after each. Wait for each to finish before the next.

```
py -3.12 -m venv .venv
```
> Creates a private Python environment for the scanner.

```
.venv\Scripts\pip install -r requirements.txt
```
> Downloads the tools the scanner needs. Takes 1–2 minutes — lots of scrolling text is normal.

```
.venv\Scripts\playwright install chromium
```
> Downloads a headless browser used to load pages. Takes another 1–2 minutes.

You're done with setup!

### Step 4 — Run the scanner

Every time you want to scan, do Step 2 first (open Command Prompt in the folder), then run:

```
.venv\Scripts\python app.py
```

You'll see:
```
Uvicorn running on http://127.0.0.1:8001
```

Open any browser and go to **http://localhost:8001**

### Stopping the scanner (Windows)

Go back to Command Prompt and press **Ctrl + C** (hold Control, press C).

---

## Using the scanner

These steps are the same on Mac and Windows.

1. Click **Start Scan** — pages appear live as they are processed.
2. Wait for the crawl to finish. The full site may take 15–30 minutes.
3. Click **Generate Report** when done.
4. A report file is saved in the `data/reports/` folder inside the project. Open it in any browser.

### Tips

- **Product codes** (like "FMB920", "GPS", "LTE") are expected English on a Japanese page — use the filter toggle to hide them from the report.
- You can stop and restart the scan at any time — it remembers where it left off.
- To start a completely fresh scan, click **Clear & Restart** before starting.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `python3.12: command not found` (Mac) | Install Python 3.12 from [python.org](https://www.python.org/downloads/) |
| `'py' is not recognized` (Windows) | Install Python 3.12, making sure to tick "Add Python to PATH" |
| Browser shows "This site can't be reached" | Make sure the scanner is still running in Terminal / Command Prompt |
| Scan stops mid-way | Restart the scanner — it will resume from where it left off |
| `No such file or directory` (Mac) or `The system cannot find the path` (Windows) | You're not in the right folder — repeat Step 2 |

---

## Need help?

Contact the developer or open an issue on GitHub.
