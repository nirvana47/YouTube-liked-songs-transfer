# YouTube Music Liked Songs Transfer

Transfer every liked YouTube Music song from Gmail/YouTube account A to account B with
[`ytmusicapi`](https://github.com/sigma67/ytmusicapi).

This project does two things:

1. Reads account A's special YouTube Music `Liked Songs` playlist.
2. Applies `LIKE` ratings to the same video IDs on account B.

Optionally, it can also create a private playlist on account B containing those songs.

> Important: `ytmusicapi` is an unofficial YouTube Music API wrapper. It is not supported
> or endorsed by Google. Use it carefully, run a dry run first, and keep auth material
> private. OAuth tokens are imported into macOS Keychain through `keyring`; local token
> files are only needed for initial setup and should remain ignored by git.

## Can This Be More Automated?

Yes. Use the OAuth workflow if you want to avoid copying browser request headers.

There is still one unavoidable manual step per Google account: Google must show you a
consent/login screen for account A and account B. That part should not be automated,
because bypassing Google login/2FA would be brittle and unsafe. The good news is that
OAuth replaces the painful DevTools header-copy process with a normal browser/device
authorization flow.

Recommended path:

1. Create one Google OAuth client for `ytmusicapi`.
2. Run `ytmusicapi oauth` once while authorizing account A.
3. Run `ytmusicapi oauth` once while authorizing account B.
4. Run the transfer script with `--auth-mode oauth`.

Browser-header auth is still documented below as a fallback.

## How It Works

`ytmusicapi` exposes YouTube Music's logged-in browser behavior:

- `YTMusic.get_liked_songs(limit=...)` reads the special liked-songs playlist, internally playlist ID `LM`.
- `YTMusic.rate_song(videoId, LikeStatus.LIKE)` thumbs-up a song on the target account.
- `YTMusic.create_playlist(...)` and `YTMusic.add_playlist_items(...)` can mirror the list into a normal playlist.

The script intentionally authenticates both accounts separately:

- Account A auth/token file: read-only source for liked songs.
- Account B auth/token file: write target for likes and optional playlist.

## Files

- `transfer_liked_songs.py`: main transfer script.
- `requirements.txt`: Python dependency pin range.
- `.env.example`: copy-paste template for OAuth client credentials.
- `.gitignore`: excludes auth files, local virtualenvs, caches, and reports.

## Security Defaults

- OAuth token JSON is stored in macOS Keychain through the Python `keyring` library when using `--auth-mode oauth`.
- The script imports an existing OAuth token file into Keychain the first time it runs, then uses a temporary `0600` file only while `ytmusicapi` is active.
- Non-dry-run account-B writes require `--confirm`. Without it, the script prompts interactively; in non-interactive shells it exits before writing.
- YouTube Music HTTP 429 rate-limit responses are retried with exponential backoff and small jitter.
- `.gitignore` covers auth artifacts, OAuth client-secret files, token files, and transfer reports.

## Requirements

- Python 3.10 or newer.
- Access to both Gmail/YouTube accounts in a browser.
- YouTube Music available for both accounts.

## Install on macOS

Copy and paste these commands from the repo directory. They install Homebrew Python if needed, create a local virtual environment, and install the app dependencies including the progress-bar UI.

```bash
# 1) Install Homebrew if you do not already have it
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 2) Install Python 3 with Homebrew
brew install python

# 3) Create and activate a local virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 4) Install dependencies
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt

# 5) Check the command help
python3 transfer_liked_songs.py --help
```

After setup, keep using the same Terminal window or reactivate the environment before each run:

```bash
cd path/to/YouTube-liked-songs-transfer
source .venv/bin/activate
```

If `pip` cannot reach PyPI from your network, retry from a network that can access PyPI:

```bash
python3 -m pip install -r requirements.txt
```

The `rich` dependency is used for real-time progress bars and cleaner terminal messages. If it is unavailable, the script falls back to plain text output.

## Recommended: OAuth Auth

OAuth avoids copying request headers from DevTools. It is the lowest-manual option
supported by current `ytmusicapi`.

### Create OAuth Credentials Once

You only do this Google Cloud Console setup once. It is not tied to account A only or
account B only. The same OAuth client ID and client secret are reused to authorize both
accounts, producing two different token files.

1. Open the [Google Cloud Console](https://console.cloud.google.com/).
2. Create or select a project.
3. Enable the YouTube Data API v3 for that project.
4. Create an OAuth Client ID.
5. Choose the application type `TVs and Limited Input devices`.
6. Save the generated client ID and client secret.

Copy the sample env file:

```bash
cp .env.example .env
```

Then edit `.env` and paste your values:

```bash
YTMUSICAPI_CLIENT_ID="your-client-id"
YTMUSICAPI_CLIENT_SECRET="your-client-secret"
```

The transfer script automatically loads `.env`. The `ytmusicapi oauth` CLI does not, so
for token creation either source the file first:

```bash
set -a
source .env
set +a
```

Or paste the client ID/secret directly into the `ytmusicapi oauth` command.

### Create Account A OAuth Token

```bash
mkdir -p auth
ytmusicapi oauth \
  --file auth/account_a_oauth.json \
  --client-id "$YTMUSICAPI_CLIENT_ID" \
  --client-secret "$YTMUSICAPI_CLIENT_SECRET"
```

Follow the browser/device authorization prompts and make sure you approve account A.

### Create Account B OAuth Token

Run the same command with a different output file. You do not create another Google
Cloud OAuth client for account B; you only authorize account B with the same OAuth
client credentials.

```bash
ytmusicapi oauth \
  --file auth/account_b_oauth.json \
  --client-id "$YTMUSICAPI_CLIENT_ID" \
  --client-secret "$YTMUSICAPI_CLIENT_SECRET"
```

Follow the prompts and make sure you approve account B. If your browser keeps choosing
the wrong Google account, use a separate browser profile, private window, or temporarily
sign out of the other account.

### Dry Run With OAuth

Run the transfer in dry-run mode first. On first run, the OAuth token files are imported
into macOS Keychain via `keyring`; subsequent OAuth runs can use the Keychain copy even
if the original ignored token files are removed.

```bash
python3 transfer_liked_songs.py \
  --auth-mode oauth \
  --source-auth auth/account_a_oauth.json \
  --target-auth auth/account_b_oauth.json \
  --dry-run
```

### Transfer With OAuth

```bash
python3 transfer_liked_songs.py \
  --auth-mode oauth \
  --source-auth auth/account_a_oauth.json \
  --target-auth auth/account_b_oauth.json \
  --skip-existing-target-likes \
  --confirm
```

If you prefer flags instead of environment variables:

```bash
python3 transfer_liked_songs.py \
  --auth-mode oauth \
  --oauth-client-id "your-client-id" \
  --oauth-client-secret "your-client-secret" \
  --source-auth auth/account_a_oauth.json \
  --target-auth auth/account_b_oauth.json \
  --dry-run
```

## Fallback: Browser-Header Auth

Use this only if you do not want to create Google OAuth credentials, or if OAuth is not
working for your account. You need one browser-auth JSON file for account A and one for
account B.

Create the auth directory:

```bash
mkdir -p auth
```

### Account A Auth

1. Open a browser tab logged into account A.
2. Go to [https://music.youtube.com](https://music.youtube.com).
3. Open developer tools.
4. Open the `Network` tab.
5. Filter for `/browse`.
6. Click around YouTube Music, such as `Library`, until you see a `POST` request to `music.youtube.com` with `browse` in the name.
7. Copy request headers:
   - Firefox: right-click request, then `Copy` -> `Copy Request Headers`.
   - Chrome/Edge: click the request, copy request headers from the Headers tab, or use `Copy as fetch (Node.js)` and keep only the headers object.
8. Run:

```bash
ytmusicapi browser --file auth/account_a.json
```

9. Paste account A's copied request headers when prompted.

On macOS, if pasting a long header block into Terminal fails, copy the headers to your
clipboard and use:

```bash
pbpaste | ytmusicapi browser --file auth/account_a.json
```

### Account B Auth

Repeat the same process while logged into account B, then write:

```bash
ytmusicapi browser --file auth/account_b.json
```

On macOS:

```bash
pbpaste | ytmusicapi browser --file auth/account_b.json
```

Make sure `auth/account_a.json` and `auth/account_b.json` are different files from
different browser sessions/accounts.

## Dry Run

Always start with a dry run:

```bash
python3 transfer_liked_songs.py --dry-run
```

This uses the default browser-header files, reads account A, deduplicates video IDs,
prints the first 25 items, and writes `transfer_report.json`. It does not change
account B.

Use a small limit while testing:

```bash
python3 transfer_liked_songs.py --dry-run --limit 25
```

## Transfer Likes

After the dry run looks right:

```bash
python3 transfer_liked_songs.py --confirm
```

This thumbs-up each source video ID on account B. The operation is effectively safe to
repeat because liking an already-liked song should leave it liked.

To reduce unnecessary writes, have the script read account B's current liked songs first:

```bash
python3 transfer_liked_songs.py --skip-existing-target-likes --confirm
```

## Duplicate and Unavailable Songs

### If a song is already liked in account B

Use `--skip-existing-target-likes`.

With this option, the script first reads account B's liked songs, compares video IDs,
and skips anything account B already likes. Those skipped items are counted in
`already_liked_on_target` in `transfer_report.json`.

Without this option, the script still calls `rate_song(videoId, LIKE)` for every unique
source track. Liking an already-liked song should be harmless, but it is extra API
traffic, so `--skip-existing-target-likes` is recommended.

### If a song exists in account A but is unavailable for account B

The script transfers by YouTube video ID, not by searching title/artist. That is good
because it avoids matching the wrong cover/remix/live version, but it also means there
may be no valid equivalent if account B cannot access that exact video ID.

If account B cannot like a track because it is deleted, private, blocked, or
region-restricted, the script records that item under `failed` in `transfer_report.json`
and continues unless you used `--fail-fast`. If playlist insertion fails, the report
records the failed playlist batch and its `video_ids`.

If YouTube Music already marks a source liked item as unavailable in account A's liked
songs response, you can skip those before attempting any write:

```bash
python3 transfer_liked_songs.py \
  --auth-mode oauth \
  --source-auth auth/account_a_oauth.json \
  --target-auth auth/account_b_oauth.json \
  --skip-existing-target-likes \
  --skip-source-unavailable \
  --confirm
```

The script intentionally does not search for replacements automatically. Replacement
matching is subjective and can easily choose the wrong recording.

## Transfer Likes and Create a Playlist

To both like the songs on account B and create a private playlist:

```bash
python3 transfer_liked_songs.py \
  --skip-existing-target-likes \
  --playlist-title "Imported liked songs from account A" \
  --confirm
```

To create only the playlist without changing thumbs-up status:

```bash
python3 transfer_liked_songs.py \
  --no-like \
  --playlist-title "Imported liked songs from account A" \
  --confirm
```

Playlist privacy can be `PRIVATE`, `UNLISTED`, or `PUBLIC`:

```bash
python3 transfer_liked_songs.py \
  --playlist-title "Imported liked songs from account A" \
  --playlist-privacy PRIVATE \
  --confirm
```

## Useful Options

```text
--source-auth PATH                 Auth JSON for account A.
--target-auth PATH                 Auth JSON for account B.
--auth-mode browser|oauth          Auth file type. Default: browser.
--oauth-client-id VALUE            OAuth client ID, or set YTMUSICAPI_CLIENT_ID.
--oauth-client-secret VALUE        OAuth client secret, or set YTMUSICAPI_CLIENT_SECRET.
--limit N                          Read at most N liked songs from account A.
--dry-run                          Preview only; do not modify account B.
--confirm                          Explicitly allow non-dry-run writes to account B.
--skip-existing-target-likes       Skip songs account B already likes.
--skip-source-unavailable          Skip tracks marked unavailable in account A response.
--no-like                          Do not apply LIKE ratings to account B.
--playlist-title TITLE             Create a playlist on account B.
--playlist-description TEXT        Description for the created playlist.
--playlist-privacy VALUE           PRIVATE, UNLISTED, or PUBLIC.
--chunk-size N                     Playlist insertion batch size.
--sleep SECONDS                    Delay between write calls.
--report PATH                      JSON report output path.
--fail-fast                        Stop on first write failure.
--keyring-service NAME             Keychain/keyring service name for OAuth token storage.
--source-keyring-account NAME      Keychain/keyring account name for account A.
--target-keyring-account NAME      Keychain/keyring account name for account B.
--rate-limit-retries N             Number of retries for HTTP 429 responses.
--rate-limit-initial-backoff SEC   Initial backoff delay for HTTP 429 retries.
```

## Report

Every run writes a JSON report. By default:

```text
transfer_report.json
```

The report includes:

- Number of source tracks found.
- Number of unique video IDs after deduplication.
- Number skipped because account B already liked them, if enabled.
- Number liked on account B.
- Optional playlist ID and playlist insertion count.
- Per-track failures with error messages.

## Troubleshooting

### `Missing auth file`

Create `auth/account_a.json` and `auth/account_b.json` using the auth steps.

### `Please provide authentication before using this function`

The script is not receiving a valid auth file. Confirm the path and recreate the file
with `ytmusicapi browser`.

### OAuth says client ID or secret is missing

Pass the OAuth credentials as flags:

```bash
python3 transfer_liked_songs.py \
  --auth-mode oauth \
  --oauth-client-id "your-client-id" \
  --oauth-client-secret "your-client-secret" \
  --source-auth auth/account_a_oauth.json \
  --target-auth auth/account_b_oauth.json \
  --dry-run
```

Or export them before running:

```bash
export YTMUSICAPI_CLIENT_ID="your-client-id"
export YTMUSICAPI_CLIENT_SECRET="your-client-secret"
```

You can also copy `.env.example` to `.env`; the transfer script loads `.env`
automatically. For the `ytmusicapi oauth` CLI, run `set -a; source .env; set +a` first.

### Account A and B appear mixed up

Regenerate both auth files:

1. Log out of extra Google accounts or use two separate browser profiles.
2. Generate account A auth while account A is active on YouTube Music.
3. Generate account B auth while account B is active on YouTube Music.
4. Run a dry run again.

### Some songs fail

Common reasons:

- The video is unavailable in account B's region.
- The video was deleted or made private.
- YouTube Music returned a transient server error.
- The item is a non-music video or unusual catalog item.

Failures are recorded in the report. You can rerun the script later. Use
`--skip-existing-target-likes` to avoid reprocessing already-liked target songs.

If failures are region/deletion/privacy related, rerunning usually will not fix those
specific songs unless availability changes later.

### Rate limiting or temporary failures

Increase the sleep interval:

```bash
python3 transfer_liked_songs.py --sleep 1.0
```

For a very large library, transfer in batches:

```bash
python3 transfer_liked_songs.py --limit 500 --sleep 1.0
```

Then rerun without `--limit` once you are comfortable with behavior.

## Security Notes

- Do not commit files under `auth/`.
- Do not commit `.env`; use `.env.example` as the safe template.
- Treat OAuth token files and browser-header JSON files like passwords.
- If you accidentally expose an auth file, log out of that YouTube/Google browser
  session or revoke related sessions from your Google account security settings.
- The auth files remain valid while the copied browser session remains valid.

## Limitations

- This transfers liked-song status by video ID. It does not copy watch history,
  recommendations, subscriptions, uploads, comments, or playlists unless you use the
  optional playlist mirror.
- YouTube Music may not expose every historical liked item if it is unavailable.
- Ordering is whatever YouTube Music returns for the liked-songs playlist.
- `ytmusicapi` is unofficial, so YouTube Music UI/API changes can break behavior.
