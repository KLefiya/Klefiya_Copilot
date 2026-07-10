import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { MantineProvider } from '@mantine/core'

// Mantine 的样式必须在自定义样式之前导入，否则会被它覆盖。
import '@mantine/core/styles.css'
import './index.css'

import App from './App.tsx'
import { theme } from './lib/theme'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <MantineProvider theme={theme} defaultColorScheme="dark" forceColorScheme="dark">
      <App />
    </MantineProvider>
  </StrictMode>,
)
