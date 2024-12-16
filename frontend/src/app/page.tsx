// frontend/src/app/page.tsx
import SchedulingInterface from '@/components/SchedulingInterface'
import MonitoringDashboard from '@/components/MonitoringDashboard'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs'

export default function Home() {
  return (
    <main className="min-h-screen bg-gray-50">
      <Tabs defaultValue="schedule" className="w-full">
        <TabsList className="w-full justify-start border-b">
          <TabsTrigger
            value="schedule"
            className="
              text-gray-900 font-semibold
              focus:outline-none
              data-[state=active]:border-b-2 data-[state=active]:border-blue-500
              hover:text-gray-700
            "
          >
            Schedule Interviews
          </TabsTrigger>
          <TabsTrigger
            value="monitor"
            className="
              text-gray-900 font-semibold
              focus:outline-none
              data-[state=active]:border-b-2 data-[state=active]:border-blue-500
              hover:text-gray-700
            "
          >
            Monitor Interviews
          </TabsTrigger>
        </TabsList>
        <TabsContent value="schedule">
          <SchedulingInterface />
        </TabsContent>
        <TabsContent value="monitor">
          <MonitoringDashboard />
        </TabsContent>
      </Tabs>
    </main>
  )
}
