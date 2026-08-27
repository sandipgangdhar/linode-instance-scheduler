import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
function allTimezones(): string[] {
  try {
    if (typeof Intl.supportedValuesOf === 'function') {
      return Intl.supportedValuesOf('timeZone')
    }
  } catch {}
  return [
    'UTC',
    'Asia/Kolkata',
    'America/New_York',
    'America/Los_Angeles',
    'Europe/London',
    'Europe/Berlin',
    'Asia/Tokyo',
    'Australia/Sydney',
  ]
}
const ALL_TIMEZONES = allTimezones()
const ALL_TIMEZONES_SET = new Set(ALL_TIMEZONES)
const canonicalizeCache = new Map<string, string | null>()
function canonicalizeTimezone(name: string): string | null {
  const cached = canonicalizeCache.get(name)
  if (cached !== undefined) return cached
  let result: string | null
  try {
    result = new Intl.DateTimeFormat('en-US', { timeZone: name }).resolvedOptions().timeZone
  } catch {
    result = null
  }
  canonicalizeCache.set(name, result)
  return result
}
function resolveToKnownTimezone(name: string): string {
  if (ALL_TIMEZONES_SET.has(name)) return name
  const canonical = canonicalizeTimezone(name)
  return canonical && ALL_TIMEZONES_SET.has(canonical) ? canonical : name
}
const IANA_REGION_PREFIXES = [
  'Africa',
  'America',
  'Antarctica',
  'Arctic',
  'Asia',
  'Atlantic',
  'Australia',
  'Europe',
  'Indian',
  'Pacific',
]
const IANA_SUBREGION_PREFIXES = Array.from(
  new Set(
    ALL_TIMEZONES.filter((tz) => tz.split('/').length === 3).map((tz) => tz.split('/').slice(0, 2).join('/')),
  ),
)
function computeMatches(trimmedQuery: string): string[] {
  const q = trimmedQuery.toLowerCase()
  const qUnderscored = q.includes('/') ? q.replace(/ /g, '_') : q
  const canonicalOfQuery =
    canonicalizeTimezone(trimmedQuery) ?? (qUnderscored !== q ? canonicalizeTimezone(qUnderscored) : null)
  const bareCityMatches = candidateZonesForBareCityName(trimmedQuery)
  return ALL_TIMEZONES.filter((tz) => {
    const tzLower = tz.toLowerCase()
    if (tzLower.includes(q) || tzLower.includes(qUnderscored)) return true
    if (canonicalOfQuery !== null && tz === canonicalOfQuery) return true
    return bareCityMatches.includes(tz)
  }).slice(0, 50)
}
function titleCaseCitySegmentCandidates(raw: string): string[] {
  const words = raw
    .replace(/[.']/g, '')
    .split(/[\s_-]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1).toLowerCase())
  if (words.length === 0) return []
  return [words.join('_'), words.join('-')]
}
function candidateZonesForBareCityName(query: string): string[] {
  if (!query || query.includes('/')) return []
  const cityCandidates = titleCaseCitySegmentCandidates(query)
  const found = new Set<string>()
  for (const prefix of [...IANA_REGION_PREFIXES, ...IANA_SUBREGION_PREFIXES]) {
    for (const city of cityCandidates) {
      const canonical = canonicalizeTimezone(`${prefix}/${city}`)
      if (canonical && ALL_TIMEZONES_SET.has(canonical)) found.add(canonical)
    }
  }
  return Array.from(found)
}
const COMMON_TIMEZONES = [
  'UTC',
  'America/New_York',
  'America/Chicago',
  'America/Denver',
  'America/Los_Angeles',
  'America/Sao_Paulo',
  'Europe/London',
  'Europe/Berlin',
  'Europe/Moscow',
  'Africa/Cairo',
  'Africa/Johannesburg',
  'Africa/Lagos',
  'Asia/Kolkata',
  'Asia/Dubai',
  'Asia/Shanghai',
  'Asia/Tokyo',
  'Asia/Singapore',
  'Australia/Sydney',
  'Pacific/Auckland',
]
  .map(resolveToKnownTimezone)
  .filter((tz) => ALL_TIMEZONES_SET.has(tz))
const offsetLabelCache = new Map<string, string>()
function currentOffsetLabel(zone: string): string {
  const key = `${zone}|${Math.floor(Date.now() / 3600000)}`
  const cached = offsetLabelCache.get(key)
  if (cached !== undefined) return cached
  let result = ''
  try {
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone: zone,
      timeZoneName: 'shortOffset',
    }).formatToParts(new Date())
    result = parts.find((p) => p.type === 'timeZoneName')?.value ?? ''
  } catch {
    result = ''
  }
  offsetLabelCache.set(key, result)
  return result
}
export function TimezoneSelect({
  value,
  onChange,
  disabled,
}: {
  value: string
  onChange: (timezone: string) => void
  disabled?: boolean
}) {
  const resolvedValue = useMemo(() => resolveToKnownTimezone(value), [value])
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState(resolvedValue)
  const containerRef = useRef<HTMLDivElement>(null)
  const [lastValue, setLastValue] = useState(value)
  if (value !== lastValue) {
    setLastValue(value)
    setQuery(resolvedValue)
  }
  const [userIsTyping, setUserIsTyping] = useState(false)
  const closeWithoutSelecting = useCallback(() => {
    setOpen(false)
    setQuery(resolvedValue)
    setUserIsTyping(false)
  }, [resolvedValue])
  useEffect(() => {
    const onClickAway = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) closeWithoutSelecting()
    }
    document.addEventListener('mousedown', onClickAway)
    return () => document.removeEventListener('mousedown', onClickAway)
  }, [closeWithoutSelecting])
  const [debouncedQuery, setDebouncedQuery] = useState(query)
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedQuery(query), 120)
    return () => clearTimeout(timer)
  }, [query])
  const searching = userIsTyping && query.trim().length > 0
  const matches = useMemo(
    () => (searching ? computeMatches(debouncedQuery.trim()) : COMMON_TIMEZONES),
    [debouncedQuery, searching],
  )
  const select = (tz: string) => {
    onChange(tz)
    setQuery(tz)
    setUserIsTyping(false)
    setOpen(false)
  }
  return (
    <div ref={containerRef} className="relative">
      <input
        className="mt-1 block w-full rounded-md border-0 py-1.5 px-3 text-sm text-slate-900 ring-1 ring-inset ring-slate-300 focus:ring-2 focus:ring-indigo-600"
        value={query}
        disabled={disabled}
        placeholder="Search e.g. Kolkata, New York, UTC…"
        onFocus={() => setOpen(true)}
        onChange={(e) => {
          setQuery(e.target.value)
          setUserIsTyping(true)
          setOpen(true)
        }}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && searching) {
            const trimmed = query.trim()
            const liveMatches = computeMatches(trimmed)
            if (liveMatches[0]) {
              e.preventDefault()
              select(liveMatches[0])
            } else if (trimmed) {
              e.preventDefault()
              select(trimmed)
            }
          }
          if (e.key === 'Escape') closeWithoutSelecting()
        }}
      />
      {open && (
        <div className="absolute z-10 mt-1 max-h-64 w-full overflow-auto rounded-md bg-white py-1 text-sm shadow-lg ring-1 ring-black/5">
          {!searching && (
            <p className="px-3 py-1.5 text-xs text-slate-400">
              Common zones — keep typing to search all {ALL_TIMEZONES.length}
            </p>
          )}
          {matches.length === 0 ? (
            <div className="px-3 py-2">
              <p className="text-slate-400">No match in the known list.</p>
              {query.trim() && (
                <button
                  type="button"
                  className="mt-1 text-left font-medium text-indigo-600 hover:underline"
                  onClick={() => select(query.trim())}
                >
                  Use “{query.trim()}” directly (if you know it's a valid IANA name)
                </button>
              )}
            </div>
          ) : (
            matches.map((tz) => (
              <button
                key={tz}
                type="button"
                className={`flex w-full items-center justify-between px-3 py-1.5 text-left hover:bg-indigo-50 ${tz === resolvedValue ? 'bg-indigo-50 font-medium text-indigo-700' : 'text-slate-700'}`}
                onClick={() => select(tz)}
              >
                <span>{tz.replace(/_/g, ' ')}</span>
                <span className="text-xs text-slate-400">{currentOffsetLabel(tz)}</span>
              </button>
            ))
          )}
        </div>
      )}
    </div>
  )
}
