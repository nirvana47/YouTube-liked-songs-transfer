# Desktop app (GUI) — the easy, double-click way

This repo includes a friendly graphical wizard so you can move your liked songs
**without touching the command line**. It is meant for personal use on macOS. It is
**unsigned** and does **not** require the App Store, code signing, or a paid Apple
Developer account.

The command-line tool (`transfer_liked_songs.py`) still works exactly as before — the
GUI is just an optional front end that reuses the same logic.

---

## How to run it

1. **One-time setup: a Google OAuth client ID + secret.**
   YouTube Music sign-in uses Google OAuth. You need to create an OAuth client
   **once** in the [Google Cloud Console → Credentials](https://console.cloud.google.com/apis/credentials):
   create an **OAuth client ID** of type **“TVs and Limited Input devices”**, and
   copy the **Client ID** and **Client secret**. That is the only thing the app
   cannot do for you. You can paste those two values into Step 1 and click
   **Save to .env** so you never have to enter them again.

   You do **not** need to create any token files by hand — the app signs both
   accounts in for you (see below).

2. **Double-click `Transfer Liked Songs.command` in Finder.**
   - The first launch creates a private Python environment next to the app and installs
     what it needs (this downloads the GUI toolkit and takes about a minute).
   - Every launch after that starts almost instantly.

3. **Follow the five steps** in the window:

   | Step | What happens |
   |------|--------------|
   | 1. Connect | Paste your Google OAuth client ID/secret, then **Sign in** to each account right inside the app — no terminal. A code and a link appear; click **Open browser**, approve with the right Google account, and the app finishes automatically. Already-signed-in accounts show **“✓ Already signed in — reuse.”** |
   | 2. Choose  | Decide whether to like the songs, create a playlist, or both — plus filters. |
   | 3. Preview | See exactly which songs will be transferred. **Nothing is changed yet.** |
   | 4. Transfer | Click **Start transfer** and watch live progress (liked OK / failed counts, and a “creating playlist…” phase). |
   | 5. Done | Review the summary — including any partial results — and open the saved report. |

Nothing is written to your target account until you explicitly click **Start transfer**
on step 4.

### Signing in (no terminal, no copy/paste of tokens)

On **Step 1** each account has its own **Sign in** button:

- Click **Sign in**. A short code and a Google link appear.
- Click **Open browser**, sign in with the **correct** Google account for that slot
  (source A vs. target B), and approve the request.
- You do **not** need to switch back and click anything — the app polls in the
  background and marks the account **✓** as soon as Google approves it.
- If you decline, the code expires, or it times out, you get a friendly message and a
  **Try again** button.

The signed-in tokens are stored so the next launch shows **“✓ Already signed in — reuse.”**
An **Advanced** option still lets you point at classic browser-header files instead.


---

## First-launch tips (macOS)

- **"Cannot verify the developer" / Gatekeeper warning:** If macOS refuses to open the
  file, **right-click (or Control-click) it in Finder and choose _Open_**, then confirm
  once. You only need to do this the first time.
- **"Permission denied":** If you re-downloaded the file, make it runnable again with:
  ```bash
  chmod +x "Transfer Liked Songs.command"
  ```
- **Python not found:** Install Python 3 from <https://www.python.org/downloads/> and try
  again. The python.org build is recommended.

---

## What gets created

- `.venv/` — the app's private Python environment (ignored by git, safe to delete; it is
  recreated automatically on the next launch).
- `transfer_report.json` — a detailed record of the most recent run (ignored by git).
- `logs/transfer-YYYYMMDD-HHMMSS.log` — a full technical log for each run, written to the
  `logs/` folder next to the app (ignored by git). The window only ever shows short,
  friendly messages; if something goes wrong, the matching log file has the full detail
  (timestamps, step markers, and complete error traces). The Done step tells you exactly
  which log file to open.

## Try it first without a real account (demo mode)

Want to see how every situation is handled — including errors — before you sign in for
real? Launch the app in **demo mode**. It uses a built-in mock account (no sign-in, no
network) and lets you pick a scenario to watch:

```bash
LSTRANSFER_DEMO=1 python gui_transfer.py
# or
python gui_transfer.py --demo
```

The window title shows **(DEMO MODE)** and Step 1 becomes a scenario picker. You can walk
through the happy path, an empty library, unavailable songs, mid-transfer rate limiting
(auto-retry), a network timeout, partial failures, a failed playlist, refused
authentication, and an expired token — and see the friendly messages, live progress,
partial results, and clean cancellation for each. Normal (non-demo) behavior is unchanged.

## Running the GUI manually (optional)

If you prefer to run it yourself instead of double-clicking:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-gui.txt
python gui_transfer.py
```

---

## `.command` vs a full `.app` bundle

This launcher is a `.command` script, not a packaged `.app`. For **personal, unsigned**
use they are close in effort, with meaningful trade-offs:

- **`.command` (what this repo uses):** a plain shell script that Finder runs on
  double-click. Zero build step, easy to read and tweak, and it bootstraps its own
  environment. It opens a small Terminal window while running (handy for seeing progress
  and errors). Best fit for personal use.
- **`.app` bundle:** looks like a "real" Mac app (custom icon, no Terminal window, shows
  in the Dock). Building one still needs no Apple Developer account for personal use, but
  it adds packaging work (a tool like `py2app`/`briefcase`, an `Info.plist`, an icon) and
  is harder to inspect and edit. Sharing it with others would still hit the same
  unsigned-app Gatekeeper prompt unless you pay for signing/notarization.

**Bottom line:** for one person on their own Mac, `.command` and an unsigned `.app` are
similar in effort but the `.command` is simpler and easier to maintain. If you later want
the polished look (icon + no Terminal window), moving to a `.app` is a small, well-trodden
next step and can be added without changing `gui_transfer.py`.
