import { useState } from 'react'
import type { SchedulerStatus } from '../api/types'
import { ActivityFeed, SchedulerBanner } from '../components/ActivityFeed'
import { PageHeader } from './DashboardLayout'
export function ActivityPage() {
  const [scheduler, setScheduler] = useState<SchedulerStatus | null>(null)
  return (
    <div>
      <PageHeader
        title="Activity"
        subtitle="What the backend did and why: every start, stop, migration and hook run, each scheduler decision, and console commands. Updates live."
      />
      <div className="space-y-4 px-8 py-6">
        <SchedulerBanner status={scheduler} />
        <ActivityFeed onScheduler={setScheduler} />
      </div>
    </div>
  )
}
