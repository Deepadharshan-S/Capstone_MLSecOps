import { useState, useEffect, useCallback, useRef } from 'react'

/**
 * useFetch — declarative GET with loading/error/refetch.
 *
 *   const { data, loading, error, refetch } = useFetch(() => datasetsApi.list(), [dep])
 *
 * Guards against state updates after unmount and against out-of-order
 * responses (only the latest request wins).
 */
export function useFetch(fetcher, deps = [], { enabled = true, initial = null } = {}) {
  const [data, setData] = useState(initial)
  const [loading, setLoading] = useState(enabled)
  const [error, setError] = useState(null)

  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  const reqIdRef = useRef(0)
  const mountedRef = useRef(true)

  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])

  const run = useCallback(async () => {
    const id = ++reqIdRef.current
    setLoading(true)
    setError(null)
    try {
      const result = await fetcherRef.current()
      if (mountedRef.current && id === reqIdRef.current) {
        setData(result)
        setLoading(false)
      }
      return result
    } catch (err) {
      if (mountedRef.current && id === reqIdRef.current) {
        setError(err)
        setLoading(false)
      }
      return null
    }
  }, [])

  useEffect(() => {
    if (!enabled) {
      setLoading(false)
      return undefined
    }
    run()
    return () => { reqIdRef.current += 1 } // invalidate in-flight on dep change
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, enabled])

  return { data, loading, error, refetch: run, setData }
}

/**
 * usePolling — repeat a fetcher on an interval while `enabled`.
 * Pauses automatically when the tab is hidden.
 */
export function usePolling(fetcher, intervalMs, { enabled = true, onData, immediate = true } = {}) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [lastUpdated, setLastUpdated] = useState(null)

  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher
  const cbRef = useRef(onData)
  cbRef.current = onData
  const mountedRef = useRef(true)
  const timerRef = useRef(null)

  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])

  const tick = useCallback(async () => {
    if (typeof document !== 'undefined' && document.hidden) return
    setLoading(true)
    try {
      const result = await fetcherRef.current()
      if (!mountedRef.current) return
      setData(result)
      setError(null)
      setLastUpdated(new Date())
      cbRef.current?.(result)
    } catch (err) {
      if (mountedRef.current) setError(err)
    } finally {
      if (mountedRef.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!enabled) return undefined
    if (immediate) tick()
    timerRef.current = setInterval(tick, intervalMs)
    return () => clearInterval(timerRef.current)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, intervalMs, immediate])

  return { data, loading, error, lastUpdated, refresh: tick, setData }
}

/**
 * useDebounced — value that only updates after `delayMs` of quiet.
 */
export function useDebounced(value, delayMs = 250) {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delayMs)
    return () => clearTimeout(t)
  }, [value, delayMs])
  return debounced
}

/**
 * useLocalStorage — state persisted in localStorage (JSON safe).
 */
export function useLocalStorage(key, initial) {
  const [value, setValue] = useState(() => {
    try {
      const raw = window.localStorage.getItem(key)
      return raw != null ? JSON.parse(raw) : initial
    } catch {
      return initial
    }
  })
  useEffect(() => {
    try { window.localStorage.setItem(key, JSON.stringify(value)) } catch { /* quota */ }
  }, [key, value])
  return [value, setValue]
}
