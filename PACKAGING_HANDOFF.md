# Packaging Handoff — Electron + Python-backend desktop app (Windows)

**Audience:** an AI agent (or engineer) packaging a *new* app that shares this codebase's
architecture: a Python backend (with the `cpce/` engine) spawned as a sidecar by an
Electron shell, distributed on Windows.

**Source of truth:** this recipe was extracted from a working build of *Pertinent Color
Decision System v1.3.2*. The proven reference files live in this repo and are cited
inline — read them when a step is ambiguous. You do **not** need to re-derive any of this.

> **One-line summary of the breakthrough:** Do not use a single "bundle everything" tool.
> Build three layers independently — PyInstaller `.exe` → `electron-packager` folder →
> (optional) Inno Setup installer — and hand-stitch them. `electron-builder` does not work
> for this architecture; `electron-packager` does.

---

## 0. Decide scope first

Answer this before building. It determines how many layers you need:

- **Headless / watcher-loop daemon (no UI)?** → You only need **Layer 1** (PyInstaller).
  Skip Electron, Vite, and Inno entirely. Ship the folder + a scheduled task / service.
- **User-facing desktop app with a UI?** → You need **Layers 1 + 2**, and **Layer 3** if
  you want a real installer rather than a portable zip.

If headless, jump to Layer 1, apply the **Runtime-writes rule** (§A) and the **spawn
reproducer** (§D), and you're done. The rest is UI-only.

---

## Layer 1 — Python backend → single `app.exe` (PyInstaller)

### Rule: build from the virtual environment, never system Python
The #1 cause of post-build `ModuleNotFoundError` is building with system Python, which is
missing deps. Activate the venv first.

```powershell
.\.venv\Scripts\Activate.ps1
cd backend
```

### Use a `.spec` file, not a raw command line
Native packages (cv2, scipy, sklearn, pymupdf, rapidfuzz, pytesseract) need `collect_all`,
not just `--hidden-import`. Copy [backend/app.spec](backend/app.spec) as your template and
edit the import list to match the new app's deps. Then:

```powershell
python -m PyInstaller app.spec --distpath ..\dist-backend --clean -y
```

Output: `dist-backend/app.exe` (expect 80–230 MB depending on deps).

### Backend code requirements (must be true before building)
These three fixes must be present in the backend or the bundle crashes silently in production:

1. **UTF-8 console** — at the very top of the entrypoint, before any print with non-ASCII:
   ```python
   import sys
   if sys.stdout.encoding != 'utf-8':
       try:
           sys.stdout.reconfigure(encoding='utf-8')
           sys.stderr.reconfigure(encoding='utf-8')
       except (AttributeError, OSError):
           pass
   ```
2. **No Flask reloader in a frozen bundle** (if using Flask) — the reloader spawns a second
   copy of the entire 200 MB bundle and races on the port:
   ```python
   frozen = getattr(sys, 'frozen', False)
   app.run(host='127.0.0.1', port=PORT, debug=not frozen, use_reloader=not frozen)
   ```
3. **Runtime-writes rule (§A below)** — applies to uploads, results, AND the `cpce/`
   engine's `AuditLogger` log dir.

---

## §A. Runtime-writes rule (the silent killer)

**The installed app's working directory is `C:\Program Files\...\` which is read-only
without admin.** Any code that writes to a path relative to cwd crashes in production —
and it will NOT reproduce when you double-click the `.exe` (see §D).

Route **every** runtime write to the user's temp area:

```python
import sys, tempfile
from pathlib import Path

if getattr(sys, 'frozen', False):
    base_dir = Path(tempfile.gettempdir()) / 'YourAppName'   # rename per app
else:
    base_dir = Path('.')

UPLOAD_FOLDER  = base_dir / 'uploads'
RESULTS_FOLDER = base_dir / 'results'
LOG_DIR        = base_dir / 'logs'
for d in (UPLOAD_FOLDER, RESULTS_FOLDER, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)
```

**CPCE engine specifically:** its `AuditLogger` defaults to a relative `logs/` dir created
in `__init__`. You must pass it an absolute dir or it crashes before the app starts:

```python
CPCEEngine(..., log_dir=str(LOG_DIR))
```

> Do **not** edit anything inside `cpce/` to fix this — pass the path in from the outside.
> (Engine package is immutable by project policy.)

---

## Layer 2 — Electron shell (UI apps only)

### Rule: electron-packager, NOT electron-builder
`electron-builder` fails on this architecture with `No JSON content found in output`
during dependency analysis (confirmed across 6+ attempts). Use `electron-packager`. It
packages the directory as-is and does not analyze the dep tree.

### main.js requirements
Use [frontend/electron/main.js](frontend/electron/main.js) as the reference. Two
non-negotiable patterns:

1. **Create the window FIRST, start the backend in parallel.** Never `await startBackend()`
   before `createWindow()` — that gives a 30s blank-screen launch.
   ```js
   app.whenReady().then(() => {
     createWindow();                       // first, always
     startBackend().catch(e => console.error('Backend failed:', e));
   });
   ```
2. **Spawn the backend with cwd = its own dir, dev/prod aware:**
   ```js
   const isDev = !app.isPackaged;
   const backendPath = isDev
     ? path.join(__dirname, '../../backend/app.py')
     : path.join(process.resourcesPath, 'backend', 'app.exe');
   backendProcess = spawn(isDev ? PYTHON : backendPath, isDev ? [backendPath] : [], {
     cwd: path.dirname(backendPath),
     env: { ...process.env, PYTHONUNBUFFERED: '1' },
   });
   ```

### package.json requirements
- `"main": "electron/main.js"`
- **Remove `"type": "module"`** — it conflicts with Electron's `require()`.

### Vite requirement (if React/Vite frontend)
Set `base: './'` in `vite.config.js`. Electron loads over `file://`, so Vite's default
absolute `/assets/...` paths give a blank white screen. Rebuild after changing.

### Axios / fetch requirement
The frontend must target the backend explicitly only inside Electron:
```js
axios.defaults.baseURL = window.electronAPI ? 'http://localhost:5000' : '';
```

### Build + stitch
```powershell
cd frontend
npm run build                              # vite build → frontend/dist

npx electron-packager . "Your App Name" `
  --platform=win32 --arch=x64 `
  --out=build-final --overwrite `
  --ignore="node_modules|src|.vite|dist-electron*|build-*"

# electron-packager will NOT bundle the backend .exe — copy it in manually:
Copy-Item ..\dist-backend\app.exe `
  "build-final\Your App Name-win32-x64\resources\backend\app.exe"
```

Final layout:
```
build-final\Your App Name-win32-x64\
  ├── Your App Name.exe          (launcher)
  └── resources\
      ├── app\                   (electron + dist)
      └── backend\app.exe        (Python sidecar — copied in by hand)
```

At this point the portable folder runs. Zip it for portable distribution, or continue to Layer 3.

---

## Layer 3 — Installer (optional, UI apps wanting a real installer)

Use [installer.iss](installer.iss) as the template (Inno Setup). Edit the `#define` block
(name, version, `SourceDir` pointing at the `build-final\...-win32-x64` folder) and compile:

```powershell
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
```

Output: `installer-output\YourApp-Setup-<version>.exe`. Note `PrivilegesRequired=admin` and
`DefaultDirName={autopf}` install into Program Files — which is exactly why §A matters.

---

## §D. The mandatory smoke test — write this BEFORE you trust any build

**Double-clicking `dist-backend/app.exe` directly is NOT a valid test.** It runs with your
shell's writable cwd, so the read-only-Program-Files crashes never appear. Reproduce how
Electron actually launches it: spawn with cwd pinned to the install dir and stderr piped.

Reference: [spawn_test2.js](spawn_test2.js). Template:

```js
const { spawn } = require('child_process');
const exe = String.raw`C:\Users\...\dist-backend\app.exe`;
const cwd = String.raw`C:\Program Files\Your App Name\resources\backend`;  // read-only
const child = spawn(exe, [], { cwd, env: process.env });
child.stdout.on('data', d => process.stdout.write('OUT: ' + d));
child.stderr.on('data', d => process.stderr.write('ERR: ' + d));   // <-- this catches the real crash
child.on('error', e => console.log('SPAWN ERROR:', e.message));
child.on('exit', c => console.log('exit:', c));
setTimeout(() => { child.kill(); process.exit(0); }, 30000);
```

Run with `node spawn_test.js`. If it prints a `PermissionError` or `ModuleNotFoundError`,
you found a production-only bug that the direct double-click hides. Electron's main.js does
**not** surface backend stderr to the user, so this script is your only window into silent
post-install crashes.

---

## Final checklist (driveable end-to-end)

- [ ] §0 scope decided (headless → Layer 1 only; UI → 1+2, +3 for installer)
- [ ] Backend: UTF-8 reconfigure present
- [ ] Backend: Flask reloader gated on `frozen` (if Flask)
- [ ] Backend: all runtime writes + CPCE `log_dir` routed to temp (§A)
- [ ] Layer 1: built from venv via `.spec`, `dist-backend/app.exe` produced
- [ ] §D spawn reproducer run with Program-Files cwd → no crash in stderr
- [ ] (UI) Layer 2: `base:'./'`, axios baseURL, window-first lifecycle, no `"type":"module"`
- [ ] (UI) electron-packager run, backend `.exe` copied into `resources/backend/`
- [ ] (UI) launched packaged app: window <2s, upload/processing works, closes clean
- [ ] (installer) Layer 3 compiled, installed to Program Files, re-ran §D-equivalent check

---

## Reference files in this repo
| File | What it proves |
|------|----------------|
| [backend/app.spec](backend/app.spec) | PyInstaller spec with `collect_all` for native deps |
| [frontend/electron/main.js](frontend/electron/main.js) | window-first + dev/prod spawn pattern |
| [installer.iss](installer.iss) | Inno Setup script |
| [spawn_test2.js](spawn_test2.js) | the Program-Files-cwd smoke test |
| [ELECTRON_PACKAGING_ISSUES.md](ELECTRON_PACKAGING_ISSUES.md) | full original debugging log (8 issues) |
</content>
</invoke>
