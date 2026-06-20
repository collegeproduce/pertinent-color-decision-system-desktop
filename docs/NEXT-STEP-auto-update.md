# Next Step (DevOps): Automatic Updates via electron-updater + GitHub Releases

**Status:** Planned — not started. To be implemented by DevOps.
**Owner:** DevOps
**Prereq:** Ship the current build first (see "Ship-now baseline" below). This work begins on top of that release.

---

## 1. Goal

Replace the manual *uninstall → download → reinstall* release process with an in-app
automatic update mechanism. When the dev team publishes a new version, installed
apps should **check** for it, **notify** the user, **download** it, and **install** it
on next restart — no manual uninstall.

Target stack: **electron-builder** (packaging) + **electron-updater** (in-app updates)
+ **GitHub Releases** (or a generic host) for distribution.

---

## 2. Current state (facts DevOps must know)

| Area | Today | Implication |
|------|-------|-------------|
| Electron packaging | `electron-packager` → `frontend/build-final/...win32-x64/` | Must be replaced by electron-builder. |
| Installer | Inno Setup (`installer.iss`) → `installer-output/PertinentColor-Setup-X.Y.Z.exe` | **Retire.** Inno does not emit `latest.yml`/`.blockmap`, which electron-updater requires. |
| electron-builder | Already in `frontend/package.json` devDependencies (`^26.8.1`) | Tooling is present; just unused. |
| Backend | PyInstaller onefile → `dist-backend/app.exe` (~208 MB), copied to `resources/backend/app.exe` | Must be shipped as electron-builder `extraResources`. Dominates update size. |
| Backend spawn | `frontend/electron/main.js`: prod path = `path.join(process.resourcesPath, 'backend', 'app.exe')`, cwd = its own dir | electron-builder must place the exe at `resources/backend/app.exe` so this path keeps working. Backend already writes runtime data to `%TEMP%`, so a read-only install dir is fine. |
| Install location | Program Files, `PrivilegesRequired=admin`, stable Inno `AppId {E4F8C6B2-…}` | Inno already **upgrades in place** (no manual uninstall needed today). Per-machine + admin means every auto-update would prompt UAC — see Decision B. |
| Versioning | Real version in `installer.iss` (1.3.4); `frontend/package.json` is stale `1.0.0` | electron-builder/updater key off `package.json` → must become the single source of truth. |

---

## 3. Decisions required BEFORE starting (BLOCKING)

These are product/owner decisions; DevOps should confirm them first because they change the config.

- **A. Repo visibility / distribution**
  - *Public GitHub repo* → `publish: github` is trivial.
  - *Private repo* → the updater would need an embedded GitHub token (extractable from the client = security risk). If source must stay private, use a **generic** provider: host `latest.yml` + installer + `.blockmap` on your own server / S3 / Cloudflare R2.
- **B. Install scope**
  - *Per-user* (`%LOCALAPPDATA%`, `perMachine: false`) → **silent background updates, no UAC** (recommended).
  - *Per-machine* (Program Files, all users) → every update prompts UAC.
- **C. Code signing**
  - *Unsigned* → works, but Windows SmartScreen warns on install/update.
  - *Signed* (OV ~$150/yr or EV ~$350/yr) → clean UX; required to avoid SmartScreen friction.

> Default recommendation if no other constraints: **Public repo (or generic host if private), per-user install, sign when budget allows (ship unsigned first is acceptable).**

---

## 4. Implementation steps

### Step 1 — Unify versioning
- Make `frontend/package.json` `version` the single source of truth (set it to the next release, e.g. `1.4.0`).
- Remove version duplication from `installer.iss` (Inno is being retired). If any script reads the version, point it at `package.json`.

### Step 2 — electron-builder config
Add a `build` block to `frontend/package.json` (or `frontend/electron-builder.yml`). electron-builder runs from `frontend/`, so the backend path is `../dist-backend/app.exe`:

```jsonc
"build": {
  "appId": "com.pertinentcolor.app",
  "productName": "Pertinent Color Decision System",
  "directories": { "output": "../installer-output", "buildResources": "build" },
  "files": ["dist/**/*", "electron/**/*", "package.json"],
  "extraResources": [
    { "from": "../dist-backend/app.exe", "to": "backend/app.exe" }
  ],
  "win": { "target": ["nsis"], "icon": "build/icon.ico" },
  "nsis": {
    "oneClick": false,
    "perMachine": false,                       // Decision B
    "allowToChangeInstallationDirectory": true,
    "createDesktopShortcut": true,
    "createStartMenuShortcut": true
  },
  "publish": [
    { "provider": "github", "owner": "<OWNER>", "repo": "<REPO>" }
    // or generic: { "provider": "generic", "url": "https://updates.example.com/pcolor/" }
  ]
}
```
- Keep the app icon used by Inno (`build/icon.ico`).
- Verify the packaged app launches **and** the backend starts from `resources/backend/app.exe` with a read-only cwd (same smoke test we already use).

### Step 3 — Replace the build script
New release build (replaces electron-packager + ISCC):
```bash
# 1. Backend (unchanged): PyInstaller → dist-backend/app.exe
# 2. Frontend:
cd frontend
npm ci
npm run build                      # vite → dist/
npx electron-builder --win --publish always   # builds NSIS + latest.yml + .blockmap, uploads to the provider
```
Output artifacts (must all be published together): the NSIS `*.exe`, `latest.yml`, and `*.blockmap`.

### Step 4 — Wire electron-updater into the app
- `cd frontend && npm i electron-updater`
- In `frontend/electron/main.js`, after the main window is created:
```js
const { autoUpdater } = require('electron-updater')
const { dialog } = require('electron')

autoUpdater.autoDownload = true
autoUpdater.checkForUpdatesAndNotify()

autoUpdater.on('update-downloaded', async (info) => {
  const { response } = await dialog.showMessageBox({
    type: 'info',
    buttons: ['Restart now', 'Later'],
    defaultId: 0,
    title: 'Update ready',
    message: `Version ${info.version} downloaded. Restart to install?`,
  })
  if (response === 0) autoUpdater.quitAndInstall()
})

autoUpdater.on('error', (err) => console.error('auto-update error:', err))
```
- Updates apply on quit; ensure the backend child process is killed on quit (already handled in `main.js`).

### Step 5 — CI/CD (GitHub Actions, `windows-latest`)
Trigger on a version tag (e.g. `v1.4.0`). The job must build the **backend first**, then electron-builder:
```yaml
# .github/workflows/release.yml (outline)
on: { push: { tags: ['v*'] } }
jobs:
  release:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5    # build backend
        with: { python-version: '3.13' }
      - run: pip install -r backend/requirements.txt pyinstaller
      - run: pyinstaller app.spec        # → dist-backend/app.exe  (must bundle assets/Tesseract)
      - uses: actions/setup-node@v4
        with: { node-version: '20' }
      - run: cd frontend && npm ci && npm run build
      - run: cd frontend && npx electron-builder --win --publish always
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}   # github provider
          # CSC_LINK / CSC_KEY_PASSWORD if signing (Decision C)
```
> Note: the PyInstaller step must reproduce the local build exactly — Python deps **and** bundled assets (Tesseract-OCR, models). Confirm `app.spec` is committed and asset paths resolve on the runner.

---

## 5. Acceptance criteria
1. `electron-builder` produces a working NSIS installer + `latest.yml` + `.blockmap`, published to the chosen provider.
2. Fresh install launches the app and the backend (`resources/backend/app.exe`) starts; analysis works.
3. **End-to-end update proof:** install `vN`, publish `vN+1`, confirm the running app detects it, downloads, prompts, and after restart is on `vN+1` — with **no manual uninstall**.
4. (If per-user) the update applies **without a UAC prompt**.
5. (If signed) no SmartScreen warning on install/update.

---

## 6. Rollout / migration note (important)
Existing users are on the **Inno** build (Program Files, per-machine). The **first**
electron-builder release is a one-time transition:
- Ship it as a normal download; users install it once (and, if switching to per-user,
  uninstall the old Inno version — its `AppId` differs from the NSIS app, so they won't
  auto-collide).
- From that version onward, electron-updater takes over and no further manual installs
  are needed.
- Communicate this single transition step in the release notes.

---

## 7. Known caveats (don't be surprised)
- **Delta updates likely won't help.** electron-updater supports `.blockmap` differential
  downloads, but PyInstaller onefile output is **not byte-reproducible**, so the 208 MB
  backend usually changes wholesale each build → expect ~full ~300 MB downloads. Acceptable
  for a modest user base; revisit only if download size becomes a problem (options: split
  the backend into a separately-versioned asset, or pin/repro the PyInstaller build).
- **Do not edit the `cpce/` engine** as part of this work — packaging only.
- Keep the backend's `%TEMP%` runtime-data behavior; it's why a read-only install dir works.

---

## 8. Ship-now baseline (what this builds on)
The current release (the tested fixes: serial previews, blank-preview fix, per-PDF
progress bar, review modal with Mark-as B&W/Color, export Save-As dialog) ships **as-is**
via the existing Inno pipeline. Auto-update is the **next** version's first feature, not a
change to what we ship now.
