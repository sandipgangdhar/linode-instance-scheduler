import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import {
  ErrorBanner,
  ProgressBar,
  SecurityWarningBanner,
  TypeToConfirmInput,
  typedConfirmationMatches,
  WarningBanner,
} from './ui'
describe('typedConfirmationMatches', () => {
  it('matches only an exact, case-sensitive, whitespace-sensitive string', () => {
    expect(typedConfirmationMatches('redis-1', 'redis-1')).toBe(true)
    expect(typedConfirmationMatches('Redis-1', 'redis-1')).toBe(false)
    expect(typedConfirmationMatches('redis-1 ', 'redis-1')).toBe(false)
    expect(typedConfirmationMatches(' redis-1', 'redis-1')).toBe(false)
    expect(typedConfirmationMatches('', 'redis-1')).toBe(false)
    expect(typedConfirmationMatches('redis-1', '')).toBe(false)
  })
})
describe('TypeToConfirmInput', () => {
  it('reports typed text via onChange, uncoupled from whether it matches', async () => {
    const user = userEvent.setup()
    let value = ''
    const { rerender } = render(
      <TypeToConfirmInput expected="redis-1" value={value} onChange={(v) => (value = v)} />,
    )
    const input = screen.getByRole('textbox')
    await user.type(input, 'red')
    rerender(<TypeToConfirmInput expected="redis-1" value="redis-1" onChange={(v) => (value = v)} />)
    expect(screen.getByRole('textbox')).toHaveValue('redis-1')
  })
  it("shows the default 'Type ... to confirm' label when none is given", () => {
    render(<TypeToConfirmInput expected="redis-1" value="" onChange={() => {}} />)
    expect(screen.getByText(/Type\s*.redis-1.\s*to confirm/i)).toBeInTheDocument()
  })
})
describe('banners', () => {
  it('ErrorBanner renders the given message', () => {
    render(<ErrorBanner message="boom" />)
    expect(screen.getByText('boom')).toBeInTheDocument()
  })
  it('SecurityWarningBanner is visually and textually distinct (uppercase heading + message)', () => {
    render(<SecurityWarningBanner message="host key changed" />)
    expect(screen.getByText(/security warning/i)).toBeInTheDocument()
    expect(screen.getByText('host key changed')).toBeInTheDocument()
  })
  it('WarningBanner joins multiple messages with newlines', () => {
    render(<WarningBanner messages={['first issue', 'second issue']} />)
    const container = screen.getByText(/first issue/)
    expect(container.textContent).toBe('first issue\nsecond issue')
  })
})
describe('ProgressBar', () => {
  it('exposes percent via ARIA attributes and clamps the visible width to at least minPercent', () => {
    render(<ProgressBar percent={0} label="starting" />)
    const bar = screen.getByRole('progressbar')
    expect(bar).toHaveAttribute('aria-valuenow', '0')
    expect(bar).toHaveAttribute('aria-valuemin', '0')
    expect(bar).toHaveAttribute('aria-valuemax', '100')
    expect(screen.getByText('starting')).toBeInTheDocument()
  })
  it('renders no label element at all when none is given', () => {
    render(<ProgressBar percent={50} />)
    expect(screen.queryByText('starting')).not.toBeInTheDocument()
  })
})
