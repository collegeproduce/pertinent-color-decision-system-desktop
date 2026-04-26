const { app, BrowserWindow } = require('electron')
const path = require('path')

let mainWindow

function createWindow() {
  console.log('Creating window...')
  
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    show: true,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true
    },
    title: 'Test Window',
    backgroundColor: '#ffffff'
  })

  console.log('Window created, loading URL...')
  
  // Just load Google as a test
  mainWindow.loadURL('https://www.google.com')
  
  mainWindow.webContents.openDevTools()
  
  mainWindow.on('closed', () => {
    mainWindow = null
  })
  
  console.log('Window setup complete')
}

app.whenReady().then(() => {
  console.log('App ready, creating window...')
  createWindow()
})

app.on('window-all-closed', () => {
  app.quit()
})

console.log('Main script loaded')
