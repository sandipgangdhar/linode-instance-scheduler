import type { ButtonHTMLAttributes, PropsWithChildren, ReactNode } from 'react'
export function Card({
  children,
  className = '',
}: PropsWithChildren<{
  className?: string
}>) {
  return (
    <div className={`rounded-lg border border-slate-200 bg-white shadow-sm ${className}`}>{children}</div>
  )
}
export function CardHeader({
  title,
  subtitle,
  action,
}: {
  title: string
  subtitle?: string
  action?: ReactNode
}) {
  return (
    <div className="flex items-start justify-between border-b border-slate-100 px-5 py-4">
      <div>
        <h2 className="text-sm font-semibold text-slate-900">{title}</h2>
        {subtitle && <p className="mt-0.5 text-sm text-slate-500">{subtitle}</p>}
      </div>
      {action}
    </div>
  )
}
type ButtonVariant = 'primary' | 'secondary' | 'danger' | 'ghost'
const VARIANT_CLASSES: Record<ButtonVariant, string> = {
  primary: 'bg-indigo-600 text-white hover:bg-indigo-500 focus-visible:outline-indigo-600',
  secondary:
    'bg-white text-slate-900 ring-1 ring-inset ring-slate-300 hover:bg-slate-50 focus-visible:outline-indigo-600',
  danger: 'bg-red-600 text-white hover:bg-red-500 focus-visible:outline-red-600',
  ghost: 'text-slate-600 hover:bg-slate-100 focus-visible:outline-indigo-600',
}
export function Button({
  variant = 'secondary',
  className = '',
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant
}) {
  return (
    <button
      className={`inline-flex items-center justify-center gap-2 rounded-md px-3 py-2 text-sm font-semibold shadow-sm transition disabled:cursor-not-allowed disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 ${VARIANT_CLASSES[variant]} ${className}`}
      {...props}
    />
  )
}
export function Spinner({ className = '' }: { className?: string }) {
  return (
    <svg className={`h-4 w-4 animate-spin ${className}`} viewBox="0 0 24 24" fill="none">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
    </svg>
  )
}
export function EmptyState({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <div className="flex flex-col items-center justify-center px-6 py-16 text-center">
      <p className="text-sm font-medium text-slate-900">{title}</p>
      {subtitle && <p className="mt-1 text-sm text-slate-500">{subtitle}</p>}
    </div>
  )
}
export function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="whitespace-pre-wrap rounded-md bg-red-50 px-4 py-3 text-sm text-red-800 ring-1 ring-inset ring-red-200">
      {message}
    </div>
  )
}
export function SecurityWarningBanner({ message }: { message: string }) {
  return (
    <div className="whitespace-pre-wrap rounded-md border-2 border-red-600 bg-red-100 px-4 py-3 text-sm font-medium text-red-900">
      <p className="mb-1 font-bold uppercase tracking-wide">Security warning</p>
      {message}
    </div>
  )
}
export function WarningBanner({ messages }: { messages: string[] }) {
  return (
    <div className="whitespace-pre-wrap rounded-md bg-amber-50 px-4 py-3 text-sm text-amber-800 ring-1 ring-inset ring-amber-200">
      {messages.join('\n')}
    </div>
  )
}
export function typedConfirmationMatches(value: string, expected: string): boolean {
  return value === expected
}
export function TypeToConfirmInput({
  id,
  expected,
  value,
  onChange,
  label,
  autoFocus,
  ringClassName = 'focus:ring-indigo-600',
}: {
  id?: string
  expected: string
  value: string
  onChange: (value: string) => void
  label?: ReactNode
  autoFocus?: boolean
  ringClassName?: string
}) {
  return (
    <label className="block" htmlFor={id}>
      <span className="text-xs font-medium text-slate-500">{label ?? <>Type “{expected}” to confirm</>}</span>
      <input
        id={id}
        autoFocus={autoFocus}
        className={`mt-1 block w-full rounded-md border-0 py-1.5 px-3 text-sm text-slate-900 ring-1 ring-inset ring-slate-300 focus:ring-2 ${ringClassName}`}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    </label>
  )
}
export function ProgressBar({
  percent,
  label,
  trackClassName = 'h-2 bg-slate-100',
  fillClassName = 'bg-indigo-600',
  minPercent = 4,
}: {
  percent: number
  label?: string | null
  trackClassName?: string
  fillClassName?: string
  minPercent?: number
}) {
  return (
    <div className="space-y-1.5">
      <div
        className={`w-full overflow-hidden rounded-full ${trackClassName}`}
        role="progressbar"
        aria-valuenow={Math.round(percent)}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <div
          className={`h-full rounded-full transition-all duration-300 ${fillClassName}`}
          style={{ width: `${Math.max(minPercent, percent)}%` }}
        />
      </div>
      {label && <p className="text-xs text-slate-500">{label}</p>}
    </div>
  )
}
