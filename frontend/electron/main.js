const { app, BrowserWindow, ipcMain } = require('electron')
const path = require('path')
const { spawn } = require('child_process')
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
      mainWindow.loadURL('http://localhost:5000').catch(e => {
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

function startBackend() {
  return new Promise((resolve, reject) => {
    console.log('Starting Python backend...')

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

function stopBackend() {
  if (backendProcess) {
    console.log('Stopping backend...')
    backendProcess.kill()
    backendProcess = null
  }
}

// App lifecycle
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

app.on('before-quit', () => {
  stopBackend()
})

// IPC handlers for frontend-backend communication
ipcMain.handle('get-backend-url', () => {
  return 'http://localhost:5000'
})

ipcMain.handle('check-backend-status', async () => {
  try {
    const response = await fetch('http://localhost:5000/api/health')
    return response.ok
  } catch {
    return false
  }
})
