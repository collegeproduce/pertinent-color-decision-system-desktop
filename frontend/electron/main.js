const { app, BrowserWindow, ipcMain } = require('electron')
const path = require('path')
const { spawn, execSync } = require('child_process')
const fs = require('fs')

let mainWindow
let backendProcess

// Path to Python backend executable (will be in resources/backend/ after packaging)
const isDev = !app.isPackaged
const backendPath = isDev
  ? path.join(__dirname, '../../backend/app.py')
  : path.join(process.resourcesPath, 'backend', 'app.exe')

const pythonPath = isDev
  ? 'c:/Users/user/Downloads/pcolor/.venv/Scripts/python.exe'
  : null // Not needed when using bundled .exe

const BACKEND_PORT = 5000

// Absolute paths to Windows system tools so process cleanup works even if PATH
// is unusual (some locked-down machines don't expose System32 on PATH).
const SYS32 = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32')
const TASKKILL = path.join(SYS32, 'taskkill.exe')
const NETSTAT = path.join(SYS32, 'netstat.exe')
const TASKLIST = path.join(SYS32, 'tasklist.exe')

// --- Single-instance lock -------------------------------------------------
// Without this, launching the app a second time spawns a RIVAL backend that
// fights the first one for port 5000 — a major source of the "failed to
// upload / analyses forever" degradation. If we don't get the lock, another
// copy already owns the backend; just focus it and exit.
const gotTheLock = app.requestSingleInstanceLock()
if (!gotTheLock) {
  app.quit()
}

function createWindow() {
  console.log('Creating window...')

  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 1200,
    minHeight: 700,
    show: true, // Show immediately
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      enableRemoteModule: false,
      // Keep the renderer (and its SSE progress stream) running at full speed
      // when the window is hidden/minimized or the user switches to another app.
      // Default is true, which throttles timers and stalls live analysis updates.
      backgroundThrottling: false
    },
    title: 'Pertinent Color Decision System',
    backgroundColor: '#f5f5f5',
    autoHideMenuBar: true
  })

  // Remove menu bar
  mainWindow.setMenu(null)

  console.log('Window created, loading content...')

  // Load the app
  if (isDev) {
    // Development: Load from Vite dev server
    mainWindow.loadURL('http://localhost:5173')
    mainWindow.webContents.openDevTools()
  } else {
    // Production: Load from built files
    const htmlPath = path.join(__dirname, '../dist/index.html')
    console.log('Loading HTML from:', htmlPath)
    console.log('HTML file exists:', fs.existsSync(htmlPath))

    mainWindow.loadFile(htmlPath).then(() => {
      console.log('✅ HTML loaded successfully')
    }).catch(err => {
      console.error('❌ Failed to load HTML:', err)
      // Fallback: show error page or load from backend
      mainWindow.loadURL(`http://localhost:${BACKEND_PORT}`).catch(e => {
        console.error('Also failed to load from backend:', e)
      })
    })

    // DevTools removed for production
    // mainWindow.webContents.openDevTools()
  }

  mainWindow.on('closed', () => {
    mainWindow = null
  })

  console.log('Window setup complete')
}

// Kill any stale backend left over from a previous session that crashed or was
// force-closed (Task Manager, OS shutdown) — those don't run our normal cleanup,
// so an orphaned app.exe can still hold port 5000 and wedge the new launch.
// Surgical: only kills a process that (a) is listening on our port AND (b) is
// named app.exe, so it can't take down unrelated software.
function reclaimBackendPort() {
  if (process.platform !== 'win32' || isDev) return
  try {
    const out = execSync(`"${NETSTAT}" -ano -p tcp`, { encoding: 'utf8' })
    const pids = new Set()
    for (const line of out.split('\n')) {
      if (line.includes(`:${BACKEND_PORT}`) && /LISTENING/i.test(line)) {
        const pid = line.trim().split(/\s+/).pop()
        if (/^\d+$/.test(pid)) pids.add(pid)
      }
    }
    for (const pid of pids) {
      try {
        const tl = execSync(`"${TASKLIST}" /fi "PID eq ${pid}" /fo csv /nh`, { encoding: 'utf8' })
        if (/app\.exe/i.test(tl)) {
          execSync(`"${TASKKILL}" /PID ${pid} /T /F`, { stdio: 'ignore' })
          console.log('Reclaimed port', BACKEND_PORT, 'from stale backend PID', pid)
        }
      } catch { /* process gone already */ }
    }
  } catch (e) {
    console.error('reclaimBackendPort failed (non-fatal):', e.message)
  }
}

function startBackend() {
  return new Promise((resolve, reject) => {
    console.log('Starting Python backend...')

    // Make sure no stale backend is squatting on the port first.
    reclaimBackendPort()

    if (isDev) {
      // Development: Run Python script directly
      console.log('Dev mode: Running Python script:', backendPath)
      backendProcess = spawn(pythonPath, [backendPath], {
        cwd: path.join(__dirname, '../../backend'),
        env: { ...process.env, PYTHONUNBUFFERED: '1' }
      })
    } else {
      // Production: Run packaged executable
      console.log('Production mode: Running backend executable:', backendPath)
      backendProcess = spawn(backendPath, [], {
        cwd: path.dirname(backendPath),
        env: { ...process.env }
      })
    }

    backendProcess.stdout.on('data', (data) => {
      console.log(`Backend: ${data.toString()}`)

      // Wait for Flask to start
      if (data.toString().includes('Running on')) {
        console.log('✅ Backend started successfully')
        resolve()
      }
    })

    backendProcess.stderr.on('data', (data) => {
      console.error(`Backend Error: ${data.toString()}`)
    })

    backendProcess.on('error', (error) => {
      console.error('Failed to start backend:', error)
      reject(error)
    })

    backendProcess.on('close', (code) => {
      console.log(`Backend process exited with code ${code}`)
    })

    // Timeout if backend doesn't start in 30 seconds
    setTimeout(() => {
      if (!backendProcess || backendProcess.exitCode !== null) {
        reject(new Error('Backend failed to start within 30 seconds'))
      }
    }, 30000)
  })
}

// Reliably terminate the WHOLE backend process tree. The backend is a
// PyInstaller onefile, which on Windows runs as a bootloader parent + a Flask
// child that holds the port. A plain .kill() only kills the parent, leaving the
// child orphaned (and the ~200MB _MEI temp dir uncleaned). taskkill /T /F takes
// down parent + child together.
function stopBackend() {
  if (!backendProcess) return
  const pid = backendProcess.pid
  try {
    if (process.platform === 'win32' && pid) {
      execSync(`"${TASKKILL}" /PID ${pid} /T /F`, { stdio: 'ignore' })
    } else {
      backendProcess.kill()
    }
  } catch (e) {
    try { backendProcess.kill() } catch { /* already gone */ }
  }
  backendProcess = null
}

// App lifecycle (only when we hold the single-instance lock)
if (gotTheLock) {
  app.on('second-instance', () => {
    // Someone tried to open a second copy — focus the existing window instead.
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore()
      mainWindow.focus()
    }
  })

  app.whenReady().then(() => {
    console.log('App ready, creating window immediately...')

    // Create window FIRST (don't wait for backend)
    createWindow()

    // Start backend in parallel
    startBackend().catch(error => {
      console.error('Backend failed to start:', error)
      // Don't quit - let user see the error in the window
    })

    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) {
        createWindow()
      }
    })
  })

  app.on('window-all-closed', () => {
    stopBackend()
    if (process.platform !== 'darwin') {
      app.quit()
    }
  })

  // Cover every shutdown path so the backend can never be orphaned.
  app.on('before-quit', stopBackend)
  app.on('will-quit', stopBackend)
  process.on('exit', stopBackend)
}

// IPC handlers for frontend-backend communication
ipcMain.handle('get-backend-url', () => {
  return `http://localhost:${BACKEND_PORT}`
})

ipcMain.handle('check-backend-status', async () => {
  try {
    const response = await fetch(`http://localhost:${BACKEND_PORT}/api/health`)
    return response.ok
  } catch {
    return false
  }
})
