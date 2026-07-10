/**
 * happy-dom 缺少两个浏览器 API，Mantine 与 recharts 都要用：
 *   matchMedia    —— Mantine 的响应式与 color scheme
 *   ResizeObserver —— Mantine ScrollArea、recharts ResponsiveContainer
 * 不 polyfill 就会在 render 时抛错，掩盖真正的组件问题。
 */

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

globalThis.ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }),
})
