# Project Backlog

This file tracks planned UX improvements, feature requests, and technical debt for future work.

## Onboarding & UX
- [ ] **Visual Google Cloud Console Setup Guide**: Create a walkthrough with screenshots and arrows covering the entire GCP setup process (Project creation, YouTube Data API v3 enablement, OAuth Client ID creation, and adding Test Users).
- [ ] **macOS .app Bundle**: Package the application as a native `.app` bundle with a custom icon, removing the dependency on a visible Terminal window during launch.
- [ ] **Dark Mode Polish**: Audit and refine UI contrasts, border colors, and widget rendering specifically for macOS Dark Mode.
- [ ] **Context-Aware Error Help**: For common errors like "Access Blocked", provide a direct link to the specific section of the Google Cloud Console (e.g., the Test Users list).

## Sign-in (Browser-header / cURL)
Browser sign-in (paste "Copy as cURL") is now the primary, recommended method because Google's OAuth *device* flow is broken upstream (ytmusicapi #682/#813). Follow-ups:
- [ ] **Raw-header paste support**: Accept pasted raw request headers (Firefox "Copy Request Headers") in addition to "Copy as cURL (bash)", so users aren't limited to one browser menu path.
- [ ] **Session-expiry detection**: Browser cookies eventually expire. Detect the resulting 401/403 during transfer and prompt the user to paste a fresh session, instead of surfacing a generic error.
- [ ] **Illustrated DevTools walkthrough**: Add screenshots/GIF for the DevTools → Network → Copy as cURL steps (currently text-only inline guidance).
- [ ] **Re-enable OAuth automatically**: Once ytmusicapi/Google restore the device flow, promote OAuth back to a first-class option and drop the "may not work" label.

## Features
- [ ] **Multi-Playlist Selection**: Allow users to transfer specific playlists in addition to the "Liked Songs" collection.
- [ ] **Transfer History**: Keep a persistent log/database of previous transfers within the GUI to allow "Incremental" updates more easily.

## Technical Debt
- [ ] **Refactor Worker Logic**: Consolidate common retry/API patterns between `transfer_liked_songs.py` and `gui_transfer.py` once the parallel hardening phase is merged.
- [ ] **Unit Tests for GUI Components**: Expand tests beyond scenario-based workers to cover UI widget states and signal connections.
