# Light Studio

Music light shows for Govee lights — Wi‑Fi strips and lamps (LAN Control) and Bluetooth bulbs.
Add a song (file or YouTube link), get an auto-generated show with separate lanes for drums, bass,
vocals and other instruments, edit it on a timeline, and play it back synced to your lights.

## Android app
Every push to `main` builds the app. To install: open **Releases** on this repo from your phone,
download the newest `LightStudio.apk`, and open it (allow "install unknown apps" for your browser
the first time). New builds install as updates and keep your songs.

- Keep Light Studio open while a show plays — the screen stays on, and switching away pauses the song.
- Bulbs over Bluetooth need the Bluetooth (and Location) permission; Android asks the first time you
  tap **Find Bluetooth lights**.
- Share a YouTube video to **Light Studio** from the YouTube app to import it.
- AI instrument separation (Demucs) is desktop-only; the app estimates instruments from frequencies.

## Desktop app
`desktop/light_studio.py` — run `python light_studio.py`. Optional extras:
`pip install bleak` (Bluetooth bulbs), `pip install yt-dlp` (YouTube links), `pip install demucs` (AI separation).

## Layout
- `web/page.html`, `web/app.js` — the editor UI, shared by both apps
- `web/desktop_server.py` — desktop server; `web/build.py` builds `desktop/light_studio.py` and the Android `index.html`
- `app/` — Android app: `core/` is the same server API in plain Java; the rest is Android glue (WebView, Bluetooth, YouTube)
