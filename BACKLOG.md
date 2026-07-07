# Project Backlog

This file tracks planned UX improvements, feature requests, and technical debt for future work.

## Onboarding & UX
- [ ] **Visual Google Cloud Console Setup Guide**: Create a walkthrough with screenshots and arrows covering the entire GCP setup process (Project creation, YouTube Data API v3 enablement, OAuth Client ID creation, and adding Test Users).
- [ ] **macOS .app Bundle**: Package the application as a native `.app` bundle with a custom icon, removing the dependency on a visible Terminal window during launch.
- [ ] **Dark Mode Polish**: Audit and refine UI contrasts, border colors, and widget rendering specifically for macOS Dark Mode.
- [ ] **Context-Aware Error Help**: For common errors like "Access Blocked", provide a direct link to the specific section of the Google Cloud Console (e.g., the Test Users list).

## Features
- [ ] **Multi-Playlist Selection**: Allow users to transfer specific playlists in addition to the "Liked Songs" collection.
- [ ] **Transfer History**: Keep a persistent log/database of previous transfers within the GUI to allow "Incremental" updates more easily.

## Technical Debt
- [ ] **Refactor Worker Logic**: Consolidate common retry/API patterns between `transfer_liked_songs.py` and `gui_transfer.py` once the parallel hardening phase is merged.
- [ ] **Unit Tests for GUI Components**: Expand tests beyond scenario-based workers to cover UI widget states and signal connections.
