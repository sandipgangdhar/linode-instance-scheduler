import { useAuth } from '../auth/AuthContext'
import { Button } from '../components/ui'
export function LoginPage() {
  const { login } = useAuth()
  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-950">
      <div className="w-full max-w-sm rounded-xl border border-slate-800 bg-slate-900 p-8 shadow-2xl">
        <div className="mb-8 flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-indigo-600 text-lg font-bold text-white">
            L
          </div>
          <div>
            <p className="text-sm font-semibold text-white">Instance Scheduler</p>
            <p className="text-xs text-slate-400">Self-hosted, per account</p>
          </div>
        </div>
        <p className="mb-6 text-sm text-slate-300">
          Sign in with the exact same credentials you already use for Linode Cloud Manager. Your password
          never touches this app — Linode authenticates you directly.
        </p>
        <Button variant="primary" className="w-full justify-center" onClick={login}>
          Login with Linode
        </Button>
      </div>
    </div>
  )
}
