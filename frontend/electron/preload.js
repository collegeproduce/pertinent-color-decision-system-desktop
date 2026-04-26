const { contextBridge, ipcRenderer } = require('electron')

// Expose protected methods to renderer process
contextBridge.exposeInMainWorld('electronAPI', {
  // Get backend URL
  getBackendUrl: () => ipcRenderer.invoke('get-backend-url'),
  
  // Check if backend is ready
  checkBackendStatus: () => ipcRenderer.invoke('check-backend-status'),
  
  // Platform info
  platform: process.platform,
  isElectron: true
})
