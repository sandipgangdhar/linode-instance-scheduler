import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  api,
  ApiError,
  cancelBackgroundPolling,
  clearStoredToken,
  errorWarnings,
  getStoredToken,
  PollingCancelledError,
  resetBackgroundPollingCancellation,
  storeToken,
} from './client'
function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}
function textResponse(status: number, text: string): Response {
  return new Response(text, { status, headers: { 'Content-Type': 'text/html' } })
}
function emptyResponse(status: number): Response {
  return new Response('', { status })
}
describe('errorWarnings', () => {
  it('extracts a non-empty warnings array from an ApiError body', () => {
    const e = new ApiError(409, 'conflict', { warnings: ['careful', 'now'] })
    expect(errorWarnings(e)).toEqual(['careful', 'now'])
  })
  it('returns null when the body has no warnings field', () => {
    const e = new ApiError(400, 'bad request', { detail: 'bad request' })
    expect(errorWarnings(e)).toBeNull()
  })
  it('returns null for an empty warnings array', () => {
    const e = new ApiError(200, 'ok', { warnings: [] })
    expect(errorWarnings(e)).toBeNull()
  })
  it('returns null for a non-ApiError value', () => {
    expect(errorWarnings(new Error('boom'))).toBeNull()
    expect(errorWarnings('boom')).toBeNull()
  })
})
describe('token storage', () => {
  beforeEach(() => localStorage.clear())
  it('round-trips through localStorage', () => {
    expect(getStoredToken()).toBeNull()
    storeToken('tok-123')
    expect(getStoredToken()).toBe('tok-123')
    clearStoredToken()
    expect(getStoredToken()).toBeNull()
  })
})
describe('request() error handling', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.stubGlobal('fetch', vi.fn())
  })
  afterEach(() => vi.unstubAllGlobals())
  it('returns parsed JSON on success', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(200, { a: 'redis-1' }))
    const result = await api.listInstances()
    expect(result).toEqual({ a: 'redis-1' })
  })
  it('clears the stored token on a 401', async () => {
    storeToken('stale-token')
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(401, { detail: 'session expired' }))
    await expect(api.listInstances()).rejects.toBeInstanceOf(ApiError)
    expect(getStoredToken()).toBeNull()
  })
  it('throws a clean ApiError (not a raw SyntaxError) for a non-JSON error body', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(textResponse(502, '<html>Bad Gateway</html>'))
    let caught: unknown
    try {
      await api.listInstances()
    } catch (e) {
      caught = e
    }
    expect(caught).toBeInstanceOf(ApiError)
    expect((caught as ApiError).status).toBe(502)
    expect((caught as ApiError).message).toContain('Bad Gateway')
    expect((caught as ApiError).body).toBeNull()
  })
  it('uses a structured `detail` field from a JSON error body as the message, and keeps the body', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(409, {
        detail: "'redis-1' is already onboarded",
        already_onboarded_name: 'redis-1',
      }),
    )
    let caught: unknown
    try {
      await api.listInstances()
    } catch (e) {
      caught = e
    }
    expect(caught).toBeInstanceOf(ApiError)
    expect((caught as ApiError).message).toBe("'redis-1' is already onboarded")
    expect((caught as ApiError).body).toEqual({
      detail: "'redis-1' is already onboarded",
      already_onboarded_name: 'redis-1',
    })
  })
  it('falls back to a generic message when there is no body and no text at all', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(emptyResponse(500))
    let caught: unknown
    try {
      await api.listInstances()
    } catch (e) {
      caught = e
    }
    expect(caught).toBeInstanceOf(ApiError)
    expect((caught as ApiError).message).toBe('request failed with status 500')
  })
  it('sends the stored bearer token on every request', async () => {
    storeToken('my-session-token')
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(200, {}))
    await api.listInstances()
    const [, init] = vi.mocked(fetch).mock.calls[0]!
    expect((init?.headers as Record<string, string>).Authorization).toBe('Bearer my-session-token')
  })
})
describe('pollOperation() (exercised via api.startInstance)', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.stubGlobal('fetch', vi.fn())
  })
  afterEach(() => vi.unstubAllGlobals())
  it('polls until done and resolves with the operation result', async () => {
    const f = vi.mocked(fetch)
    f.mockResolvedValueOnce(jsonResponse(200, { operation_id: 'op-1' }))
    f.mockResolvedValueOnce(
      jsonResponse(200, { status: 'running', percent: 30, current_step: 'creating', warnings: [] }),
    )
    f.mockResolvedValueOnce(
      jsonResponse(200, {
        status: 'done',
        percent: 100,
        current_step: null,
        warnings: [],
        result: { outcome: 'started' },
      }),
    )
    const progressCalls: Array<[number, string | null]> = []
    const result = await api.startInstance('redis-1', undefined, (p, s) => progressCalls.push([p, s]))
    expect(result).toEqual({ outcome: 'started' })
    expect(f).toHaveBeenCalledTimes(3)
    expect(progressCalls).toEqual([
      [30, 'creating'],
      [100, null],
    ])
  })
  it('skips onProgress when percent and current_step are unchanged between polls', async () => {
    const f = vi.mocked(fetch)
    f.mockResolvedValueOnce(jsonResponse(200, { operation_id: 'op-1' }))
    f.mockResolvedValueOnce(
      jsonResponse(200, { status: 'running', percent: 50, current_step: 'booting', warnings: [] }),
    )
    f.mockResolvedValueOnce(
      jsonResponse(200, { status: 'running', percent: 50, current_step: 'booting', warnings: [] }),
    )
    f.mockResolvedValueOnce(
      jsonResponse(200, {
        status: 'done',
        percent: 100,
        current_step: null,
        warnings: [],
        result: { outcome: 'started' },
      }),
    )
    const progressCalls: Array<[number, string | null]> = []
    await api.startInstance('redis-1', undefined, (p, s) => progressCalls.push([p, s]))
    expect(progressCalls).toEqual([
      [50, 'booting'],
      [100, null],
    ])
  })
  it('calls onWarning exactly once, with the full accumulated array, right before resolving', async () => {
    const f = vi.mocked(fetch)
    f.mockResolvedValueOnce(jsonResponse(200, { operation_id: 'op-1' }))
    f.mockResolvedValueOnce(
      jsonResponse(200, {
        status: 'done',
        percent: 100,
        current_step: null,
        warnings: ['tag refresh degraded: could not verify write'],
        result: { outcome: 'stopped' },
      }),
    )
    const warningCalls: string[][] = []
    await api.stopInstance('redis-1', undefined, undefined, (w) => warningCalls.push(w))
    expect(warningCalls).toEqual([['tag refresh degraded: could not verify write']])
  })
  it('does not call onWarning at all when the final warnings array is empty', async () => {
    const f = vi.mocked(fetch)
    f.mockResolvedValueOnce(jsonResponse(200, { operation_id: 'op-1' }))
    f.mockResolvedValueOnce(
      jsonResponse(200, {
        status: 'done',
        percent: 100,
        current_step: null,
        warnings: [],
        result: { outcome: 'stopped' },
      }),
    )
    const warningCalls: string[][] = []
    await api.stopInstance('redis-1', undefined, undefined, (w) => warningCalls.push(w))
    expect(warningCalls).toEqual([])
  })
  it('propagates a genuinely failed operation as an ApiError carrying its warnings', async () => {
    const f = vi.mocked(fetch)
    f.mockResolvedValueOnce(jsonResponse(200, { operation_id: 'op-1' }))
    f.mockResolvedValueOnce(
      jsonResponse(409, {
        detail: "resource ownership conflict for 'redis-1'",
        warnings: ['identity changed: os_volume_id 111 -> 222'],
      }),
    )
    let caught: unknown
    try {
      await api.startInstance('redis-1')
    } catch (e) {
      caught = e
    }
    expect(caught).toBeInstanceOf(ApiError)
    expect((caught as ApiError).status).toBe(409)
    expect(errorWarnings(caught)).toEqual(['identity changed: os_volume_id 111 -> 222'])
  })
})
describe('background polling cancellation (logout leak fix)', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.stubGlobal('fetch', vi.fn())
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    resetBackgroundPollingCancellation()
  })
  it('stops the poll loop and rejects once cancelBackgroundPolling() is called, without polling again', async () => {
    const f = vi.mocked(fetch)
    f.mockResolvedValueOnce(jsonResponse(200, { operation_id: 'op-1' }))
    f.mockResolvedValueOnce(
      jsonResponse(200, { status: 'running', percent: 30, current_step: 'creating', warnings: [] }),
    )
    const progressCalls: Array<[number, string | null]> = []
    const promise = api.startInstance('redis-1', undefined, (p, s) => progressCalls.push([p, s]))
    await vi.waitFor(() => expect(progressCalls).toEqual([[30, 'creating']]))
    cancelBackgroundPolling()
    await expect(promise).rejects.toThrow(PollingCancelledError)
    expect(f).toHaveBeenCalledTimes(2)
  })
  it('a call made while already cancelled never issues even the kickoff poll', async () => {
    cancelBackgroundPolling()
    const f = vi.mocked(fetch)
    f.mockResolvedValueOnce(jsonResponse(200, { operation_id: 'op-1' }))
    await expect(api.startInstance('redis-1')).rejects.toThrow(PollingCancelledError)
    expect(f).toHaveBeenCalledTimes(1)
  })
  it('resetBackgroundPollingCancellation() re-arms polling for a fresh session', async () => {
    cancelBackgroundPolling()
    resetBackgroundPollingCancellation()
    const f = vi.mocked(fetch)
    f.mockResolvedValueOnce(jsonResponse(200, { operation_id: 'op-1' }))
    f.mockResolvedValueOnce(
      jsonResponse(200, {
        status: 'done',
        percent: 100,
        current_step: null,
        warnings: [],
        result: { outcome: 'started' },
      }),
    )
    const result = await api.startInstance('redis-1')
    expect(result).toEqual({ outcome: 'started' })
  })
})
