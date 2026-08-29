export interface LsblkDevice {
  name: string
  sizeMb: number
  type: string | null
}
const SIZE_UNIT_MULTIPLIERS_TO_MB: Record<string, number> = {
  B: 1 / (1024 * 1024),
  K: 1 / 1024,
  M: 1,
  G: 1024,
  T: 1024 * 1024,
}
function parseLsblkSizeToMb(raw: string): number | null {
  const m = /^([\d.]+)\s*([BKMGT])?$/i.exec(raw.trim())
  if (!m) return null
  const value = parseFloat(m[1])
  if (Number.isNaN(value)) return null
  const unit = (m[2] ?? 'M').toUpperCase()
  const multiplier = SIZE_UNIT_MULTIPLIERS_TO_MB[unit]
  if (multiplier === undefined) return null
  return value * multiplier
}
export function parseLsblkDevices(text: string): LsblkDevice[] {
  const devices: LsblkDevice[] = []
  for (const rawLine of text.split('\n')) {
    const line = rawLine.replace(/^[\s│├└─]+/, '')
    const cols = line.trim().split(/\s+/)
    if (cols.length < 4) continue
    const [name, majMin, , sizeRaw, , typeRaw] = cols
    if (!/^\d+:\d+$/.test(majMin)) continue
    if (!/^[a-z]+[a-z0-9]*$/i.test(name)) continue
    const sizeMb = parseLsblkSizeToMb(sizeRaw)
    if (sizeMb === null) continue
    devices.push({ name, sizeMb, type: typeRaw ?? null })
  }
  return devices
}
export interface DeviceGuess {
  source: string | null
  destination: string | null
}
export function guessSourceAndDestination(
  devices: LsblkDevice[],
  localDiskSizeMb: number | null,
  destVolumeSizeGb: number | null,
): DeviceGuess {
  if (localDiskSizeMb == null || destVolumeSizeGb == null) return { source: null, destination: null }
  const expectedSourceMb = localDiskSizeMb
  const expectedDestMb = destVolumeSizeGb * 1024
  const wholeDisks = devices.filter((d) => d.type === 'disk')
  const candidates = wholeDisks.length > 0 ? wholeDisks : devices
  const distTo = (value: number, ref: number) => Math.abs(value - ref)
  const plausible = (value: number, ref: number) => distTo(value, ref) <= 0.6 * Math.max(value, ref)
  const bySourceDistance = [...candidates].sort(
    (a, b) => distTo(a.sizeMb, expectedSourceMb) - distTo(b.sizeMb, expectedSourceMb),
  )
  const byDestDistance = [...candidates].sort(
    (a, b) => distTo(a.sizeMb, expectedDestMb) - distTo(b.sizeMb, expectedDestMb),
  )
  const sourceGuess = bySourceDistance[0]
  const destGuess = byDestDistance[0]
  const sourceUnique =
    bySourceDistance.length < 2 ||
    distTo(bySourceDistance[0].sizeMb, expectedSourceMb) <
      distTo(bySourceDistance[1].sizeMb, expectedSourceMb)
  const destUnique =
    byDestDistance.length < 2 ||
    distTo(byDestDistance[0].sizeMb, expectedDestMb) < distTo(byDestDistance[1].sizeMb, expectedDestMb)
  const sourceOk = sourceGuess && sourceUnique && plausible(sourceGuess.sizeMb, expectedSourceMb)
  const destOk =
    destGuess &&
    destUnique &&
    plausible(destGuess.sizeMb, expectedDestMb) &&
    destGuess.name !== sourceGuess?.name
  return {
    source: sourceOk ? sourceGuess!.name : null,
    destination: destOk ? destGuess!.name : null,
  }
}
export function buildDdCommand(sourceDevice: string, destinationDevice: string): string {
  return `dd if=/dev/${sourceDevice} of=/dev/${destinationDevice} bs=4M status=progress && sync`
}
export type DdOutputVerification =
  | {
      status: 'inconclusive'
    }
  | {
      status: 'looks_successful'
    }
  | {
      status: 'looks_failed'
      detail: string
    }
const DD_ERROR_PATTERN =
  /\b(error|cannot open|permission denied|no space left|input\/output error|invalid argument|no such device|operation not permitted)\b/i
function parseEchoedDdCommand(text: string): {
  source: string
  destination: string
} | null {
  const m = /dd\s+if=(\S+)\s+of=(\S+)/.exec(text)
  if (!m) return null
  const stripDev = (p: string) => p.replace(/^\/dev\//, '')
  return { source: stripDev(m[1]), destination: stripDev(m[2]) }
}
export function verifyDdOutput(
  text: string,
  expectedSource: string,
  expectedDestination: string,
): DdOutputVerification {
  if (!text.trim()) return { status: 'inconclusive' }
  const echoed = parseEchoedDdCommand(text)
  if (echoed && (echoed.source !== expectedSource || echoed.destination !== expectedDestination)) {
    return {
      status: 'looks_failed',
      detail:
        `The command in your output was \`dd if=/dev/${echoed.source} of=/dev/${echoed.destination}\`, ` +
        `but this page built \`dd if=/dev/${expectedSource} of=/dev/${expectedDestination}\` -- these ` +
        "don't match. Run the exact command shown above, then paste its own output.",
    }
  }
  const errorMatch = DD_ERROR_PATTERN.exec(text)
  if (errorMatch) {
    const line = text
      .split('\n')
      .find((l) => DD_ERROR_PATTERN.test(l))
      ?.trim()
    return { status: 'looks_failed', detail: line ?? `Found "${errorMatch[0]}" in the output.` }
  }
  const inMatch = /(\d+\+\d+)\s+records in/.exec(text)
  const outMatch = /(\d+\+\d+)\s+records out/.exec(text)
  if (!inMatch || !outMatch) return { status: 'inconclusive' }
  if (inMatch[1] !== outMatch[1]) {
    return {
      status: 'looks_failed',
      detail: `"${inMatch[1]} records in" doesn't match "${outMatch[1]} records out" -- not everything read may have been written.`,
    }
  }
  return { status: 'looks_successful' }
}
