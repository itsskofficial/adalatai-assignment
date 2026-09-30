import { PackageOpenIcon } from 'lucide-react'
import { EmptyState } from './components/EmptyState'
import { PageHeader, Screen } from './components/Screen'

export function NotBuiltYet({ name }: { name: string }) {
  return (
    <Screen>
      <PageHeader title={name} />
      <EmptyState icon={PackageOpenIcon}>Not built yet.</EmptyState>
    </Screen>
  )
}
